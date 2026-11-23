"""游戏文本包（`gtd-bundle/1`）：找包、校验、读表。

包由 game-text-data 项目 `uv run gametext <游戏> bundle` 出（协议在那边 `docs/architecture.md` 的"产物包协议"一节），一款游戏一个：
`<game>-<version>-<指纹前 12 位>.zip`，里面 `<game>/manifest.json` + 数据文件 + `report.json` + `NOTICE.txt`。
不公开分发，用户私下拿到后放进 `paths.gametext_root()`（默认数据根的 `gametext/`）即可，**不用解压**；解压了也认。

- **找**：`--gametext` / `FLOWOCR_GAMETEXT` / 默认位置可以是一个 `.zip`、一个带 `manifest.json` 的目录（开发机直接指
  `../game-text-data/corpus/<游戏>`），或一个装着若干包的目录（按清单的 `game` 挑；解压出的目录往下找两层，见 `candidates`）。
  同一款游戏找到指纹不同的多份就报错列出来，不按版本号猜；指纹相同的几份是同一份语料，用排在前面的那份。
  没有清单的目录不认——不留旧格式的读法。
- **校验**：清单 `schema` 要认得、要有调用方要的表和语种（`ja` / `zh-Hans`）、指纹按清单重算要对上；
  调用方要的每张表还要带调用方认得的**内容契约主版本**（清单 `tables.<表>.contract`，形如 `genshin.reminders/1`，
  字段的类型、能否为 null、取值写在 game-text-data 那边的契约声明里）：缺 `contract` 是加契约之前打的旧包，
  主版本不同是字段含义变了，都拒绝；每个读到的文件
  边读边算存储字节的 `sha256` 与解压后的 `content_sha256`，读完和清单比。缓存命中不读数据时用 `verify_stored`
  只核存储字节（每款几十 MB）。哈希防的是拷坏和版本对不上，不是安全机制。
- **身份**：下游只认 `fingerprint`（按内容算，换压缩、改文件名都不变）。`provenance()` 是剧本里记的那一份。
  清单的 `rev` 是 game-text-data 发布时记进指纹表的提取行为版本（整数；本地包是 `<rev>+<指纹前 8 位>`），
  只抄下来给人看——核"是不是维护者发出的那份"是拿指纹去对那边公开的指纹表，这里不联网、不校验它。
  命令行（`main`）打印指纹之前先 `verify` 全部文件的两种哈希，否则内容被换过的包也能打印出一个对得上表的指纹。
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from flowocr import paths

SCHEMA = "gtd-bundle/1"
MANIFEST = "manifest.json"
CHUNK = 1 << 20


class BundleError(SystemExit):
    """找不到包、包坏了、和清单对不上。继承 SystemExit：CLI 直接带着这句话退出，守卫里照样能 catch。"""


def fingerprint_of(files: list[dict]) -> str:
    """协议里的指纹：按 `(table, lang)` 排序的 `table\\tlang\\tcontent_sha256\\n` 串接的 sha256，只算数据表。"""
    items = sorted((f["table"], f["lang"], f["content_sha256"]) for f in files if "table" in f)
    return "sha256:" + hashlib.sha256("".join("%s\t%s\t%s\n" % it for it in items).encode("utf-8")).hexdigest()


class _Tee:
    """读的同时喂哈希（给 zstd 的 stream_reader 当源）。"""

    def __init__(self, f, h):
        self.f, self.h = f, h

    def read(self, n: int = -1) -> bytes:
        b = self.f.read(n)
        self.h.update(b)
        return b


@dataclass
class Bundle:
    source: Path          # .zip 或目录
    manifest: dict
    prefix: str = ""      # zip 里的成员前缀（`<game>/`）；目录形态为空

    @property
    def game(self) -> str:
        return self.manifest["game"]

    @property
    def fingerprint(self) -> str:
        return self.manifest["fingerprint"]

    def provenance(self) -> dict:
        """剧本 provenance 里的 `gametext`。`source` 只记文件 / 目录名，不记绝对路径（产物会被分享）。"""
        m = self.manifest
        up = m.get("upstream") or {}
        return {"game": m["game"], "fingerprint": m["fingerprint"], "rev": m.get("rev"), "version": up.get("version"),
                "upstream": {k: up.get(k) for k in ("repo", "commit", "version")},
                "extractor": m.get("extractor") or {},
                "integrity_checked": bool(up.get("integrity_checked")),
                "source": self.source.name}

    def _files(self) -> dict[tuple[str, str], dict]:
        return {(f["table"], f["lang"]): f for f in self.manifest["files"] if "table" in f}

    def _open(self, rel: str):
        if self.source.suffix.lower() == ".zip":
            zf = zipfile.ZipFile(self.source)
            try:
                raw = zf.open(self.prefix + rel)
            except KeyError:
                zf.close()
                raise BundleError(f"文本包 {self.source.name} 里缺 {self.prefix + rel}（清单里有）")
            return _Closing(raw, zf)
        p = self.source / rel
        if not p.is_file():
            raise BundleError(f"文本包 {self.source} 里缺 {rel}（清单里有）")
        return open(p, "rb")

    def has(self, tables, langs=("ja", "zh-Hans")) -> bool:
        """可选的表：这几张表 × 语种都在清单里就 True（旧包没有的表调用方自己跳过，不报错）。"""
        have = self._files()
        return all((t, lang) in have for t in tables for lang in langs)

    def require(self, tables: dict[str, int], langs=("ja", "zh-Hans")) -> None:
        """调用方要的表（{表: 认得的契约主版本}）× 语种都得在清单里，每张表的契约主版本要对上——
        缺了就报缺什么（`--langs chs` 收窄跑出的包会缺日文），版本不对就报是哪张表、包里是几、这里要几。"""
        have = self._files()
        miss = [f"{t}×{lang}" for t in tables for lang in langs if (t, lang) not in have]
        if miss:
            raise BundleError(f"文本包 {self.source.name}（{self.game}）缺 {', '.join(miss)}："
                              f"清单里的语种是 {sorted(self.manifest.get('langs') or {})}")
        for t, major in tables.items():
            label = ((self.manifest.get("tables") or {}).get(t) or {}).get("contract")
            if label is None:
                raise BundleError(f"文本包 {self.source.name}（{self.game}）的 {t} 没有内容契约版本：这是加契约之前打的包，"
                                  f"game-text-data 那边用新代码重新 bundle")
            if label != f"{self.game}.{t}/{major}":
                raise BundleError(f"文本包 {self.source.name} 的 {t} 是契约 {label}，这里只认 {self.game}.{t}/{major}："
                                  f"字段含义变了，flowocr 要先跟上（看 game-text-data 的 CHANGELOG）")

    def _chunks(self, f: dict):
        """逐块给出清单里这个文件解压后的字节，边读边算两种哈希；**读完**和清单比，对不上就报错
        （读到一半就停的调用方不做这次核对）。解压 / zip 的 CRC 出错都算"包坏了"，不让用户看底层栈。"""
        import zstandard
        raw_h, con_h = hashlib.sha256(), hashlib.sha256()
        zst = f["path"].endswith(".zst")
        try:
            with self._open(f["path"]) as raw:
                src = _Tee(raw, raw_h)
                stream = zstandard.ZstdDecompressor().stream_reader(src) if zst else src
                for chunk in iter(lambda: stream.read(CHUNK), b""):
                    if zst:
                        con_h.update(chunk)
                    yield chunk
                while src.read(CHUNK):      # zstd 帧后面若还有字节，也要进存储哈希
                    pass
        except (zstandard.ZstdError, zipfile.BadZipFile) as exc:
            raise self._broken(f, exc)
        content = con_h if zst else raw_h
        if raw_h.hexdigest() != f["sha256"] or content.hexdigest() != f.get("content_sha256", f["sha256"]):
            raise BundleError(f"文本包 {self.source.name} 的 {f['path']} 和清单对不上（sha256）——包坏了或被改过，重新拿一份")

    def _broken(self, f: dict, exc: Exception) -> BundleError:
        return BundleError(f"文本包 {self.source.name} 的 {f['path']} 读不下去（{type(exc).__name__}: {exc}）"
                           f"——包坏了或被改过，重新拿一份")

    def iter_rows(self, table: str, lang: str):
        """逐行读一张表（`_chunks`：边读边核两种哈希）。JSON 解码出错也算包坏了（UnicodeDecodeError 是 ValueError）。"""
        f = self._files()[(table, lang)]
        pending = b""
        try:
            for chunk in self._chunks(f):
                lines = (pending + chunk).split(b"\n")
                pending = lines.pop()
                rows = [json.loads(ln) for ln in lines if ln.strip()]
                yield from rows
            if pending.strip():
                yield json.loads(pending)
        except ValueError as exc:
            raise self._broken(f, exc)

    def verify(self) -> None:
        """清单里的每个文件都核一遍：存储字节的 `sha256`，数据文件再核解压后的 `content_sha256`。
        拿指纹去对指纹表之前用——只核存储字节的话，改了内容、同时改掉清单里存储哈希的包照样过，
        而指纹（只由内容哈希算）还和表对得上。"""
        for f in self.manifest["files"]:
            for _ in self._chunks(f):
                pass

    def verify_stored(self, tables, langs=("ja", "zh-Hans")) -> None:
        """只核存储字节的 sha256（缓存命中、不解压时用）。"""
        have = self._files()
        for t in tables:
            for lang in langs:
                f = have[(t, lang)]
                h = hashlib.sha256()
                with self._open(f["path"]) as raw:
                    for chunk in iter(lambda: raw.read(CHUNK), b""):
                        h.update(chunk)
                if h.hexdigest() != f["sha256"]:
                    raise BundleError(f"文本包 {self.source.name} 的 {f['path']} 和清单对不上（sha256）——包坏了或被改过，重新拿一份")


class _Closing(io.RawIOBase):
    """zip 成员 + 它的 ZipFile，一起关。"""

    def __init__(self, raw, zf):
        self.raw, self.zf = raw, zf

    def read(self, n: int = -1) -> bytes:
        return self.raw.read(n)

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        try:
            self.raw.close()
        finally:
            self.zf.close()
            super().close()


def _load_manifest(text: str, where: str) -> dict:
    try:
        m = json.loads(text)
    except ValueError as exc:
        raise BundleError(f"{where} 的清单不是合法 JSON：{exc}")
    if m.get("schema") != SCHEMA:
        raise BundleError(f"{where} 的清单 schema 是 {m.get('schema')!r}，这里只认 {SCHEMA}（game-text-data 那边重新 bundle）")
    for k in ("game", "fingerprint", "files", "langs"):
        if k not in m:
            raise BundleError(f"{where} 的清单缺 {k}")
    if fingerprint_of(m["files"]) != m["fingerprint"]:
        raise BundleError(f"{where} 的清单自相矛盾：按文件重算的指纹不是它记的 {m['fingerprint']}")
    return m


def open_bundle(p: Path) -> Bundle:
    """一个包：`.zip`，或带 `manifest.json` 的目录。"""
    if p.is_file() and p.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(p) as zf:
                names = [n for n in zf.namelist() if n.count("/") == 1 and n.endswith("/" + MANIFEST)]
                if len(names) != 1:
                    raise BundleError(f"{p.name} 不是文本包：顶层目录下应当恰好一份 {MANIFEST}，找到 {len(names)} 份")
                m = _load_manifest(zf.read(names[0]).decode("utf-8"), p.name)
                prefix = names[0][: -len(MANIFEST)]
        except zipfile.BadZipFile as exc:
            raise BundleError(f"{p} 不是有效的 zip：{exc}")
        if prefix != m["game"] + "/":
            raise BundleError(f"{p.name}：成员目录 {prefix!r} 和清单的 game {m['game']!r} 不一致")
        return Bundle(p, m, prefix)
    if (p / MANIFEST).is_file():
        return Bundle(p, _load_manifest((p / MANIFEST).read_text(encoding="utf-8"), str(p)))
    raise BundleError(f"{p} 既不是文本包（.zip），也不是带 {MANIFEST} 的目录")


def _has_manifest(p: Path) -> bool:
    try:
        with zipfile.ZipFile(p) as zf:
            return any(n.count("/") == 1 and n.endswith("/" + MANIFEST) for n in zf.namelist())
    except zipfile.BadZipFile:
        return False


def candidates(root: Path) -> list[Path]:
    """`root` 下可能是包的东西：它自己；或它下面一层的 `.zip` 与带清单的子目录，以及再下一层带清单的目录——
    包解压出来是 `<game>/manifest.json`，而 Windows 的"全部解压缩"默认再套一层以 zip 命名的文件夹。"""
    if root.is_file() or (root / MANIFEST).is_file():
        return [root]
    if not root.is_dir():
        return []
    found = list(root.glob("*.zip"))
    for q in root.iterdir():
        if not q.is_dir():
            continue
        if (q / MANIFEST).is_file():
            found.append(q)
        else:
            found += [s for s in q.iterdir() if s.is_dir() and (s / MANIFEST).is_file()]
    return sorted(found)


def locate(game: str, where: str | Path | None = None) -> Bundle:
    """找 `game` 的包。`where` 不给就用 `paths.gametext_root()`。"""
    root = Path(where).expanduser().resolve() if where else paths.gametext_root()
    bundles = []
    for q in candidates(root):
        if q.is_file() and q.suffix.lower() == ".zip" and not _has_manifest(q):
            # 只给人看的一行（驱动里 same_ref 那段也会打出来）：别拿 stdout 解析
            print(f"  [文本包] 跳过 {q.name}：不是文本包（顶层目录下没有 {MANIFEST}）", file=sys.stderr, flush=True)
            continue
        bundles.append(open_bundle(q))          # 有清单但清单坏了的照样报错
    found = [b for b in bundles if b.game == game]
    if not found:
        raise BundleError(f"没找到 {game} 的游戏文本包（在 {root} 找的）。包从 game-text-data 的 `bundle` 来，"
                          f"放进这个目录即可，或用 --gametext / FLOWOCR_GAMETEXT 指过去")
    # 指纹相同就是同一份语料（例如 zip 和它解压出的目录并存），用排在前面的那份；内容不同才要用户挑
    if len({b.fingerprint for b in found}) > 1:
        listing = [f"{b.source.name}（{b.fingerprint.removeprefix('sha256:')[:12]}）" for b in found]
        raise BundleError(f"{root} 下 {game} 的文本包有 {len(found)} 份、内容不同：{listing}——"
                          f"用 --gametext 指定其中一份")
    return found[0]


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog=f"{paths.python_cmd()} -m flowocr.analyze.gtdbundle",   # 帮助里的写法 uv tool 装的也能照抄
                                 description="找游戏文本包、核对包里每个文件的哈希，再打印它的身份（指纹 / 位置 / 全部来源信息）")
    ap.add_argument("game")
    ap.add_argument("--gametext", default=None, help="包 / 包目录（默认 paths.gametext_root()）")
    ap.add_argument("--field", choices=("fingerprint", "source", "json"), default="fingerprint")
    a = ap.parse_args(argv)
    b = locate(a.game, a.gametext)
    b.verify()      # 打印出的指纹是拿去对指纹表的，先证明包里的内容就是清单说的那份
    print(json.dumps(b.provenance(), ensure_ascii=False) if a.field == "json"
          else b.fingerprint if a.field == "fingerprint" else str(b.source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
