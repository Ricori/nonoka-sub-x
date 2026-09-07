// 让用户选一份字幕文档。
//
// media.list 读的是**本地**媒体库，`documentAvailable` 查的是本地文件
// documents/<id>/document.json。云端跑完的字幕在「取回本地」之前不在这个列表里，
// 所以空列表最常见的原因不是「还没转写」，而是云端结果还没落到本机 —— 这两种
// 情况的提示要分开写，否则用户会以为插件坏了。

/**
 * 「有视频、没字幕」的条目该怎么办。
 *
 * 插件**没有能力**去云端取字幕：领取走的是宿主前端的 cloudAccount.adoptLibraryEntry，
 * 不在插件能力表里，我们连云端有哪些条目都看不见。所以「重新读取」永远只是重读
 * 本地媒体库，点多少次都不会把云端的结果拉下来。
 *
 * 宿主自己会自动领取（本地有视频、没字幕、指纹和云端某条已完成记录对上），但触发
 * 时机是云端列表刷新 —— 应用启动、云端任务跑完被侦测到、重新登录，都不包括用户在
 * 这个插件里点按钮。所以只能把该做什么说出来。
 */
const PENDING_HINT = "如果云端已经转写完，重启应用或回主页刷新媒体库，宿主会自动取回字幕。";

/** 有本地视频但还没有字幕 —— 正好是宿主自动领取会去认领的那种形状。 */
function pendingCloud(items: MediaSummary[]): number {
  return items.filter((item) => item.available && !item.documentAvailable).length;
}

/** 已经读到的媒体，按 media.list 的原始顺序。 */
let catalogue: MediaSummary[] = [];

/**
 * 当前选中的媒体，没选中时是 null。
 *
 * 直接从 <select> 派生而不是另存一份：多存一份就要靠 change 事件同步，
 * 而「忘了同步」和「同步时机不对」是这类界面最常见的 bug。DOM 本来就是真相。
 */
function selectedMedia(): MediaSummary | null {
  const id = el<HTMLSelectElement>("media").value;
  return catalogue.find((item) => item.id === id) ?? null;
}

async function loadMedia(): Promise<void> {
  const select = el<HTMLSelectElement>("media");
  const reloadButton = el<HTMLButtonElement>("reload");
  const startButton = el<HTMLButtonElement>("start");

  // 读取期间三个控件一起锁：连点「重新读取」会并发跑两次、状态栏文字来回跳；
  // 列表还没填好就点「开始」，selectedMedia() 拿到的是上一批里的条目。
  // 解锁放在 finally，所以下面每个提前 return 的分支都会恢复。
  select.disabled = true;
  reloadButton.disabled = true;
  startButton.disabled = true;
  say("正在读取媒体库…");
  try {
    await fillMedia(select);
  } catch (error) {
    select.replaceChildren(new Option("读取失败", ""));
    say(`读取媒体库失败：${(error as Error).message}`, true);
  } finally {
    select.disabled = false;
    reloadButton.disabled = false;
    startButton.disabled = false;
  }
}

async function fillMedia(select: HTMLSelectElement): Promise<void> {
  catalogue = await rpc<MediaSummary[]>("media.list");
  const usable = catalogue.filter((item) => item.documentAvailable);

  if (catalogue.length === 0) {
    select.replaceChildren(new Option("本地媒体库是空的", ""));
    say(
      "本地媒体库里没有任何条目。如果你的字幕是云端跑的，先回主页对那个视频点一次"
      + "「编辑字幕」把它取回本地（会建一个没有视频文件的占位条目），再回来点「重新读取」。",
      true,
    );
    return;
  }

  if (usable.length === 0) {
    select.replaceChildren(new Option(`这 ${catalogue.length} 个条目都还没有字幕`, ""));
    say(`本地有 ${catalogue.length} 个媒体，但都还没有字幕文档。${PENDING_HINT}`, true);
    return;
  }

  // 没字幕的也列出来但置灰：只显示可用项的话，用户看到一个短列表无法判断是
  // 「别的都没字幕」还是「插件没读全」。
  select.replaceChildren(...catalogue.map((item) => {
    const option = new Option(describe(item), item.id);
    option.disabled = !item.documentAvailable;
    return option;
  }));
  select.value = usable[0]!.id;

  // 「只有字幕、没有本地视频」是云端取回的正常形态，特意说一句，
  // 否则用户看到自己没有那个视频文件会以为选错了。
  const withoutVideo = usable.filter((item) => !item.available).length;
  const tail = withoutVideo > 0 ? `（其中 ${withoutVideo} 个只有字幕、没有本地视频，不影响使用）` : "";
  const waiting = pendingCloud(catalogue);
  say(`共 ${catalogue.length} 个条目，${usable.length} 个有字幕可选${tail}。`
    + (waiting > 0 ? ` 另有 ${waiting} 个有视频没字幕 —— ${PENDING_HINT}` : ""));
}

function describe(item: MediaSummary): string {
  const mark = item.documentAvailable ? "" : " · 无字幕";
  return `${item.title}（${clock(item.duration)}）${mark}`;
}
