import assert from "node:assert/strict";
import test from "node:test";

import {
  addRange, intersectRange, joinPieces, normalizePieces, outToSrc, pieceAt, piecesDuration,
  splitAt, srcToOut, subtractRange,
} from "../src/subtitles/pieces.ts";
import { buildSrtFrom, clipAss, piecesAss } from "../src/subtitles/build.ts";

const P = [{ t0: 10, t1: 20 }, { t0: 30, t1: 35 }];

test("normalizePieces sorts, clamps, merges overlaps and keeps split points", () => {
  assert.deepEqual(normalizePieces([
    { t0: 30, t1: 40 }, { t0: -5, t1: 10 }, { t0: 10.02, t1: 12 },
    { t0: 11, t1: 11.03 }, { t0: 38, t1: 100 }, { t0: 60, t1: 50 },
  ], 45), [{ t0: 0, t1: 10 }, { t0: 10, t1: 12 }, { t0: 30, t1: 45 }]);
  assert.deepEqual(joinPieces([{ t0: 0, t1: 10 }, { t0: 10, t1: 12 }, { t0: 30, t1: 45 }]),
    [{ t0: 0, t1: 12 }, { t0: 30, t1: 45 }]);
});

test("srcToOut collapses gaps and outToSrc inverts inside pieces", () => {
  assert.equal(srcToOut(P, 5), 0);
  assert.equal(srcToOut(P, 15), 5);
  assert.equal(srcToOut(P, 25), 10);
  assert.equal(srcToOut(P, 32), 12);
  assert.equal(srcToOut(P, 99), 15);
  assert.equal(outToSrc(P, 5), 15);
  assert.equal(outToSrc(P, 10), 30);   // join belongs to the next piece
  assert.equal(outToSrc(P, 15), 35);
  assert.equal(piecesDuration(P), 15);
  assert.equal(pieceAt(P, 20), -1);
  assert.equal(pieceAt(P, 30), 1);
});

test("range edits subtract, intersect, restore and split", () => {
  assert.deepEqual(subtractRange(P, 12, 14), [{ t0: 10, t1: 12 }, { t0: 14, t1: 20 }, { t0: 30, t1: 35 }]);
  assert.deepEqual(subtractRange(P, 18, 32), [{ t0: 10, t1: 18 }, { t0: 32, t1: 35 }]);
  assert.deepEqual(intersectRange(P, 15, 31), [{ t0: 15, t1: 20 }, { t0: 30, t1: 31 }]);
  // restoring keeps every split point; merging is a separate, explicit step
  assert.deepEqual(addRange(P, 18, 40), [{ t0: 10, t1: 20 }, { t0: 20, t1: 30 }, { t0: 30, t1: 35 }, { t0: 35, t1: 40 }]);
  assert.deepEqual(joinPieces(addRange([{ t0: 0, t1: 10 }, { t0: 20, t1: 60 }], 10, 20)), [{ t0: 0, t1: 60 }]);
  assert.deepEqual(splitAt(P, 12), [{ t0: 10, t1: 12 }, { t0: 12, t1: 20 }, { t0: 30, t1: 35 }]);
  assert.deepEqual(splitAt(P, 25), P);
  assert.deepEqual(splitAt(P, 10.01), P);
});

const ASS = "[Events]\nFormat: x\n"
  + "Dialogue: 0,0:00:11.00,0:00:13.00,A,,0,0,0,,keep\n"
  + "Dialogue: 0,0:00:21.00,0:00:25.00,A,,0,0,0,,gone\n"
  + "Dialogue: 0,0:00:19.00,0:00:31.00,A,,0,0,0,,spans\n";

test("piecesAss maps every Dialogue into output time", () => {
  assert.equal(piecesAss(ASS, P), "[Events]\nFormat: x\n"
    + "Dialogue: 0,0:00:01.00,0:00:03.00,A,,0,0,0,,keep\n"
    + "Dialogue: 0,0:00:09.00,0:00:11.00,A,,0,0,0,,spans\n");
  assert.equal(clipAss(ASS, 12, 20), "[Events]\nFormat: x\n"
    + "Dialogue: 0,0:00:00.00,0:00:01.00,A,,0,0,0,,keep\n"
    + "Dialogue: 0,0:00:07.00,0:00:08.00,A,,0,0,0,,spans\n");
});

test("buildSrtFrom maps cues through pieces and drops removed ones", () => {
  const source = {
    segs: [
      { t0: 11, t1: 13, ja: "a", zh: "" },
      { t0: 21, t1: 25, ja: "b", zh: "" },
      { t0: 31, t1: 33, ja: "c", zh: "" },
    ],
    tracks: [], trackMeta: null, effects: [],
  };
  assert.equal(buildSrtFrom(source, "ja", P),
    "1\n00:00:01,000 --> 00:00:03,000\na\n\n2\n00:00:11,000 --> 00:00:13,000\nc\n\n");
});
