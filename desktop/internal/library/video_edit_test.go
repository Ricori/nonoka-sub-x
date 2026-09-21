package library

import (
	"reflect"
	"strings"
	"testing"
)

func TestNormalizePiecesMergesAndClamps(t *testing.T) {
	got := normalizePieces([]Range{
		{T0: 30, T1: 40},
		{T0: -5, T1: 10},
		{T0: 10.02, T1: 12}, // gap under minPieceSeconds snaps shut, split kept
		{T0: 11, T1: 11.03}, // sliver dropped
		{T0: 38, T1: 100},   // overlap merges, end clamps
		{T0: 60, T1: 50},    // inverted dropped
	}, 45)
	want := []Range{{T0: 0, T1: 10}, {T0: 10, T1: 12}, {T0: 30, T1: 45}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("pieces = %#v", got)
	}
	if joined := joinPieces(got); !reflect.DeepEqual(joined, []Range{{T0: 0, T1: 12}, {T0: 30, T1: 45}}) {
		t.Fatalf("joined = %#v", joined)
	}
}

func TestPiecesFilterGraph(t *testing.T) {
	graph := piecesFilterGraph([]Range{{T0: 10, T1: 12}, {T0: 15, T1: 20}}, 10, true, "subtitles=subtitle.ass")
	for _, part := range []string{
		"[0:v]trim=start=0.000:end=2.000,setpts=PTS-STARTPTS[v0];",
		"[0:v]trim=start=5.000:end=10.000,setpts=PTS-STARTPTS[v1];",
		"[0:a]atrim=start=5.000:end=10.000,asetpts=PTS-STARTPTS,afade=t=in:d=0.010,afade=t=out:st=4.990:d=0.010[a1];",
		"[v0][a0][v1][a1]concat=n=2:v=1:a=1[vc][ac];[vc]subtitles=subtitle.ass[vo]",
	} {
		if !strings.Contains(graph, part) {
			t.Fatalf("graph missing %q:\n%s", part, graph)
		}
	}
	silent := piecesFilterGraph([]Range{{T0: 0, T1: 1}, {T0: 2, T1: 3}}, 0, false, "null")
	if strings.Contains(silent, "atrim") || !strings.Contains(silent, "[v0][v1]concat=n=2:v=1:a=0[vc];[vc]null[vo]") {
		t.Fatalf("silent graph = %s", silent)
	}
}

func TestFilmstripArgsPicksSamplingMode(t *testing.T) {
	dense := strings.Join(filmstripArgs("in.mp4", "out.jpg", 10, 8, 100, 8), " ")
	if !strings.Contains(dense, "-ss 10.500 -t 8.000 -i in.mp4") || !strings.Contains(dense, "fps=1/1.000,scale=-2:54,setsar=1,tile=8x1") {
		t.Fatalf("dense args = %s", dense)
	}
	// A dense range that overruns the media seeks per frame and pulls the tail back in.
	tail := strings.Join(filmstripArgs("in.mp4", "out.jpg", 96, 8, 100, 8), " ")
	if strings.Contains(tail, "fps=") || !strings.Contains(tail, "-ss 99.900 -i in.mp4") {
		t.Fatalf("tail args = %s", tail)
	}
	sparse := strings.Join(filmstripArgs("in.mp4", "out.jpg", 90, 20, 100, 2), " ")
	// The second slot's middle (105s) lies past the media end and is pulled back.
	if !strings.Contains(sparse, "-ss 95.000 -i in.mp4 -ss 99.900 -i in.mp4") || !strings.Contains(sparse, "[f0][f1]hstack=inputs=2") {
		t.Fatalf("sparse args = %s", sparse)
	}
}
