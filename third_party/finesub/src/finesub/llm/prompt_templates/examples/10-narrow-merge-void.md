---
id: narrow-merge-void
kind: mini
applies: [capableB, capableC, basicA, basicB]
headers: true
teach: 合并行先检查末源是否更宜留给后句；缩窄用 void 而非重排
---
缩窄合并反例（translated 内）：

<input>
head | 120.0 | 1.5 | 世界に溶け込もうと
mid  | 121.7 | 2.2 | その子はいっぱい時間をかけて
tail | 124.0 | 1.4 | 他の人の真似をしたけど
</input>

<output>
# {ref:head} 句意未完、{ref:mid} 紧接，同一人连续发声，先按同一句合并
sub | head,mid | AUTO | AUTO | 世界に溶け込もうとその子はいっぱい時間をかけて | 为了融入世界，那孩子花了大量时间 | median | AUTO | 局部序号{ref:mid}更宜与{ref:tail}合并；缩窄范围 <void>
sub | head | AUTO | AUTO | 世界に溶け込もうと | 为了融入世界 | high | AUTO |
# 缩窄后 {ref:mid} 与 {ref:tail} 才是同一句（花时间去模仿），合并后 {calc:span:mid,tail}s 在界内
sub | mid,tail | AUTO | AUTO | その子はいっぱい時間をかけて他の人の真似をしたけど | 那孩子花了大量时间去模仿其他人 | high | AUTO | 缩窄后同一句
</output>
