// @wailsio/runtime 在模块初始化时就装窗口拖拽的监听，需要一整套浏览器全局。
// 单元测试跑的是纯逻辑，一次 RPC 都不发；真调到了就该立刻炸出来，而不是悄悄返回假数据。
const unavailable = () => { throw new Error("Wails runtime is not available under node --test"); };

export const Call = { ByID: unavailable, ByName: unavailable };
export const Create = {
  Any: (value) => value,
  ByteSlice: (value) => value,
  Array: () => (value) => value,
  Map: () => (value) => value,
  Nullable: () => (value) => value,
  Struct: () => (value) => value,
};
export const Events = { Emit: () => {}, On: () => () => {}, Off: () => {} };
export const System = { IsWindows: () => true, IsMac: () => false, IsLinux: () => false };
export const Window = { Get: unavailable };
export const Browser = { OpenURL: unavailable };
export class CancellablePromise extends Promise {}
