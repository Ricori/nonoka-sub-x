// 「停止」的状态。跨层用：main 设置，analysis 和 llm 检查。
//
// 中断不了正在飞的那次调用 —— 宿主没给 llm.complete 提供取消，那次请求会跑完
// 并照常消耗一次额度。所以语义是「当前这次调用返回后停下」。

/** 用它当 Error.message 的哨兵值，让上层能把「用户停止」和「真失败」分开。 */
const CANCELLED = "__cancelled__";

let cancelRequested = false;

function resetCancel(): void {
  cancelRequested = false;
}

function requestCancel(): void {
  cancelRequested = true;
}

function isCancelled(): boolean {
  return cancelRequested;
}

function checkCancel(): void {
  if (cancelRequested) throw new Error(CANCELLED);
}
