import test from "node:test";
import assert from "node:assert/strict";
import { docStore, laneNames } from "../src/editor/store/docStore.ts";

test("a translation document keeps the source and translation names", () => {
  docStore.set({ correctOnly: false });
  assert.deepEqual(laneNames(), { ja: "原文", zh: "译文", both: "双语" });
});

test("a correct-only document names its lanes transcript and polish", () => {
  docStore.set({ correctOnly: true });
  assert.deepEqual(laneNames(), { ja: "转写", zh: "润色", both: "转写 + 润色" });
  docStore.set({ correctOnly: false });
});
