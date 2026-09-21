package library

import (
	"encoding/base64"
	"errors"
	"fmt"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	"github.com/Ricori/nonoka-x/desktop/internal/managedtools"
)

const (
	maxVideoPieces = 2000
	// Pieces shorter than this are dropped and gaps shorter than this are
	// closed: a two-frame sliver on either side of a join only shows up as a
	// flash in the output.
	minPieceSeconds = 0.05
	// Each join fades the audio in and out over this long to avoid clicks.
	joinFadeSeconds = 0.01

	filmstripFrameHeight = 54
	maxFilmstripFrames   = 32
	// Slots at least this wide seek once per frame; narrower ones decode the
	// whole range once, which is cheaper when frames are close together.
	filmstripSeekStep = 3.0
)

// Range is a half-open span of source media time in seconds.
type Range struct {
	T0 float64 `json:"t0"`
	T1 float64 `json:"t1"`
}

// VideoEdit is the editor's video track: the pieces of the source that make it
// into the output, in output order. The subtitles keep source time; export
// maps them through these pieces. An empty list means the untouched source.
type VideoEdit struct {
	Pieces []Range `json:"pieces"`
}

type FilmstripTileResult struct {
	URL      string  `json:"url"`
	Start    float64 `json:"start"`
	Duration float64 `json:"duration"`
	Frames   int     `json:"frames"`
}

func (s *Service) GetVideoEdit(id string) VideoEdit {
	if !validID(id) {
		return VideoEdit{Pieces: []Range{}}
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, entry := range s.entries {
		if entry.ID == id && entry.VideoEdit != nil {
			return VideoEdit{Pieces: append([]Range{}, entry.VideoEdit.Pieces...)}
		}
	}
	return VideoEdit{Pieces: []Range{}}
}

func (s *Service) SetVideoEdit(id string, edit VideoEdit) (bool, error) {
	if !validID(id) {
		return false, errors.New("invalid media id")
	}
	s.mu.Lock()
	for index := range s.entries {
		if s.entries[index].ID != id {
			continue
		}
		pieces := normalizePieces(edit.Pieces, s.entries[index].Duration)
		previous := s.entries[index].VideoEdit
		if len(pieces) == 0 {
			s.entries[index].VideoEdit = nil
		} else {
			s.entries[index].VideoEdit = &VideoEdit{Pieces: pieces}
		}
		if err := s.saveLocked(); err != nil {
			s.entries[index].VideoEdit = previous
			s.mu.Unlock()
			return false, err
		}
		s.mu.Unlock()
		s.emitChanged()
		return true, nil
	}
	s.mu.Unlock()
	return false, errors.New("media entry not found")
}

// normalizePieces sorts, clamps and merges overlapping pieces. Pieces that
// merely touch stay separate: they are split points the editor selects and
// deletes one by one. Gaps narrower than minPieceSeconds snap shut. duration
// <= 0 means unknown and skips the upper clamp. Mirrors subtitles/pieces.ts.
func normalizePieces(pieces []Range, duration float64) []Range {
	if len(pieces) > maxVideoPieces {
		pieces = pieces[:maxVideoPieces]
	}
	valid := make([]Range, 0, len(pieces))
	for _, piece := range pieces {
		if !finite(piece.T0) || !finite(piece.T1) {
			continue
		}
		piece.T0 = math.Max(0, piece.T0)
		if duration > 0 {
			piece.T1 = math.Min(duration, piece.T1)
		}
		if piece.T1-piece.T0 < minPieceSeconds {
			continue
		}
		piece.T0 = math.Round(piece.T0*1000) / 1000
		piece.T1 = math.Round(piece.T1*1000) / 1000
		valid = append(valid, piece)
	}
	sort.Slice(valid, func(a, b int) bool { return valid[a].T0 < valid[b].T0 })
	merged := make([]Range, 0, len(valid))
	for _, piece := range valid {
		last := len(merged) - 1
		if last >= 0 && piece.T0 < merged[last].T1 {
			merged[last].T1 = math.Max(merged[last].T1, piece.T1)
			continue
		}
		if last >= 0 && piece.T0-merged[last].T1 < minPieceSeconds {
			piece.T0 = merged[last].T1
		}
		if piece.T1-piece.T0 >= minPieceSeconds {
			merged = append(merged, piece)
		}
	}
	return merged
}

// joinPieces fuses pieces that touch. A split point must not become a join in
// the output, where the audio fade would dip the sound.
func joinPieces(pieces []Range) []Range {
	joined := make([]Range, 0, len(pieces))
	for _, piece := range pieces {
		if last := len(joined) - 1; last >= 0 && piece.T0-joined[last].T1 < minPieceSeconds {
			joined[last].T1 = math.Max(joined[last].T1, piece.T1)
			continue
		}
		joined = append(joined, piece)
	}
	return joined
}

func piecesDuration(pieces []Range) float64 {
	total := 0.0
	for _, piece := range pieces {
		total += piece.T1 - piece.T0
	}
	return total
}

// piecesFilterGraph trims every piece out of an input that was opened with
// -ss offset, concatenates them, and burns the subtitles onto the joined
// stream. The subtitle file is already in output time; subtitles is the whole
// post-concat video chain (subtitles filter, fonts dir, scaling).
func piecesFilterGraph(pieces []Range, offset float64, hasAudio bool, subtitles string) string {
	var graph strings.Builder
	inputs := strings.Builder{}
	for index, piece := range pieces {
		start := formatFloat(piece.T0 - offset)
		end := formatFloat(piece.T1 - offset)
		fmt.Fprintf(&graph, "[0:v]trim=start=%s:end=%s,setpts=PTS-STARTPTS[v%d];", start, end, index)
		fmt.Fprintf(&inputs, "[v%d]", index)
		if hasAudio {
			length := piece.T1 - piece.T0
			fade := math.Min(joinFadeSeconds, length/4)
			fmt.Fprintf(&graph,
				"[0:a]atrim=start=%s:end=%s,asetpts=PTS-STARTPTS,afade=t=in:d=%s,afade=t=out:st=%s:d=%s[a%d];",
				start, end, formatFloat(fade), formatFloat(length-fade), formatFloat(fade), index)
			fmt.Fprintf(&inputs, "[a%d]", index)
		}
	}
	audio := 0
	if hasAudio {
		audio = 1
	}
	fmt.Fprintf(&graph, "%sconcat=n=%d:v=1:a=%d[vc]", inputs.String(), len(pieces), audio)
	if hasAudio {
		graph.WriteString("[ac]")
	}
	fmt.Fprintf(&graph, ";[vc]%s[vo]", subtitles)
	return graph.String()
}

// FilmstripTile renders frames evenly spread over [start, start+duration) as
// one horizontal JPEG strip, cached per range and frame count.
func (s *Service) FilmstripTile(id string, start, duration float64, frames int) (FilmstripTileResult, error) {
	if !validID(id) || !finite(start) || !finite(duration) || start < 0 || duration < .1 || duration > 6000 ||
		frames < 1 || frames > maxFilmstripFrames {
		return FilmstripTileResult{}, errors.New("invalid filmstrip range")
	}
	entry, err := s.entryByID(id)
	if err != nil {
		return FilmstripTileResult{}, err
	}
	source := s.cachedPath(id)
	if source == "" {
		source = entry.SourcePath
	}
	if !fileExists(source) {
		return FilmstripTileResult{}, errors.New("filmstrip requires local media")
	}
	if start >= entry.Duration {
		return FilmstripTileResult{}, errors.New("filmstrip range is outside media")
	}
	start = math.Round(start*1000) / 1000
	duration = math.Round(duration*1000) / 1000

	ffmpeg, err := managedtools.Find(s.root, "ffmpeg")
	if err != nil {
		return FilmstripTileResult{}, errors.New("ffmpeg is required to generate filmstrips")
	}
	directory := filepath.Join(s.root, "temporary", "filmstrip", id)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return FilmstripTileResult{}, err
	}
	output := filepath.Join(directory, fmt.Sprintf("v1-%d-%d-%d.jpg", int64(start*1000), int64(duration*1000), frames))
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	if !fileExists(output) {
		command := exec.Command(ffmpeg, filmstripArgs(source, output, start, duration, entry.Duration, frames)...)
		configureMediaCommand(command)
		if result, runErr := command.CombinedOutput(); runErr != nil {
			_ = os.Remove(output)
			return FilmstripTileResult{}, fmt.Errorf("generate filmstrip: %s", strings.TrimSpace(string(result)))
		}
	}
	data, err := os.ReadFile(output)
	if err != nil {
		return FilmstripTileResult{}, err
	}
	return FilmstripTileResult{
		URL:   "data:image/jpeg;base64," + base64.StdEncoding.EncodeToString(data),
		Start: start, Duration: duration, Frames: frames,
	}, nil
}

// filmstripArgs samples the middle of each of frames equal slots. Wide slots
// open the source once per frame with a fast input seek and stack the frames;
// narrow ones decode the range once and let fps pick the frames. A range that
// runs past the end of the media always seeks per frame: fps would run out of
// input before the last slot and tile would never emit, while the seek path
// pulls late samples back inside the media.
func filmstripArgs(source, output string, start, duration, mediaDuration float64, frames int) []string {
	step := duration / float64(frames)
	scale := fmt.Sprintf("scale=-2:%d,setsar=1", filmstripFrameHeight)
	args := []string{"-v", "error"}
	if step < filmstripSeekStep && start+duration <= mediaDuration {
		args = append(args,
			"-ss", formatFloat(start+step/2), "-t", formatFloat(duration), "-i", source,
			"-an", "-sn", "-vf", fmt.Sprintf("fps=1/%s,%s,tile=%dx1", formatFloat(step), scale, frames),
		)
	} else {
		var graph, stack strings.Builder
		for index := 0; index < frames; index++ {
			// A seek past the last frame yields nothing and hstack would stall.
			at := math.Min(start+step*(float64(index)+.5), math.Max(0, mediaDuration-.1))
			args = append(args, "-ss", formatFloat(at), "-i", source)
			fmt.Fprintf(&graph, "[%d:v]trim=end_frame=1,%s[f%d];", index, scale, index)
			fmt.Fprintf(&stack, "[f%d]", index)
		}
		if frames == 1 {
			fmt.Fprintf(&graph, "[f0]null")
		} else {
			fmt.Fprintf(&graph, "%shstack=inputs=%d", stack.String(), frames)
		}
		args = append(args, "-an", "-sn", "-filter_complex", graph.String())
	}
	return append(args, "-frames:v", "1", "-q:v", "5", "-y", output)
}
