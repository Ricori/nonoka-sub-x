package library

import (
	"context"
	"encoding/json"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/managedtools"
)

// ExportSource describes the stream the export window encodes from, so it can
// guess the output size before the encode starts. Zero means unknown.
type ExportSource struct {
	Width     int     `json:"width"`
	Height    int     `json:"height"`
	FPS       float64 `json:"fps"`
	VideoKbps float64 `json:"videoKbps"`
	AudioKbps float64 `json:"audioKbps"`
	HasAudio  bool    `json:"hasAudio"`
}

// assumedAudioKbps stands in for an audio stream whose bitrate the container
// does not record (common in MKV without statistics tags).
const assumedAudioKbps = 160

// ProbeExportSource reads the bitrates of the file a video export would
// encode from. It never fails: whatever ffprobe cannot tell is filled from the
// library entry, and what neither knows stays zero.
func ProbeExportSource(s *Service, id string) ExportSource {
	entry, err := s.entryByID(id)
	if err != nil {
		return ExportSource{}
	}
	source := s.cachedPath(id)
	if source == "" {
		source = entry.SourcePath
	}
	info := ExportSource{Width: entry.Width, Height: entry.Height, HasAudio: true}
	totalKbps := 0.0
	if entry.Duration > 0 && entry.Size > 0 && source == entry.SourcePath {
		totalKbps = float64(entry.Size) * 8 / entry.Duration / 1000
	}
	if fileExists(source) {
		if probed, total, ok := s.probeExportSource(source); ok {
			info = probed
			if total > 0 {
				totalKbps = total
			}
		}
	}
	if info.VideoKbps == 0 && totalKbps > 0 {
		audio := 0.0
		if info.HasAudio {
			audio = info.AudioKbps
			if audio == 0 {
				audio = assumedAudioKbps
			}
		}
		info.VideoKbps = max(totalKbps-audio, totalKbps*0.3)
	}
	return info
}

func (s *Service) probeExportSource(path string) (ExportSource, float64, bool) {
	executable, err := managedtools.Find(s.root, "ffprobe")
	if err != nil {
		return ExportSource{}, 0, false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, executable,
		"-v", "error",
		"-show_entries", "format=bit_rate:stream=codec_type,width,height,avg_frame_rate,bit_rate:stream_tags=BPS,BPS-eng:stream_disposition=attached_pic",
		"-of", "json", path,
	)
	configureMediaCommand(command)
	output, err := command.Output()
	if err != nil {
		return ExportSource{}, 0, false
	}
	var value struct {
		Format struct {
			BitRate string `json:"bit_rate"`
		} `json:"format"`
		Streams []struct {
			CodecType    string            `json:"codec_type"`
			Width        int               `json:"width"`
			Height       int               `json:"height"`
			AvgFrameRate string            `json:"avg_frame_rate"`
			BitRate      string            `json:"bit_rate"`
			Tags         map[string]string `json:"tags"`
			Disposition  struct {
				AttachedPic int `json:"attached_pic"`
			} `json:"disposition"`
		} `json:"streams"`
	}
	if err := json.Unmarshal(output, &value); err != nil {
		return ExportSource{}, 0, false
	}
	var info ExportSource
	seenVideo := false
	for _, stream := range value.Streams {
		// MKV keeps per-stream bitrates in mkvmerge's statistics tags instead.
		kbps := parseKbps(stream.BitRate)
		if kbps == 0 {
			kbps = parseKbps(stream.Tags["BPS"])
		}
		if kbps == 0 {
			kbps = parseKbps(stream.Tags["BPS-eng"])
		}
		switch strings.ToLower(stream.CodecType) {
		case "video":
			if stream.Disposition.AttachedPic != 0 || seenVideo {
				continue
			}
			seenVideo = true
			info.Width, info.Height = stream.Width, stream.Height
			info.FPS = parseFrameRate(stream.AvgFrameRate)
			info.VideoKbps = kbps
		case "audio":
			if info.HasAudio {
				continue
			}
			info.HasAudio = true
			info.AudioKbps = kbps
		}
	}
	return info, parseKbps(value.Format.BitRate), seenVideo
}

func parseKbps(text string) float64 {
	bits, err := strconv.ParseFloat(strings.TrimSpace(text), 64)
	if err != nil || !finite(bits) || bits <= 0 {
		return 0
	}
	return bits / 1000
}

// parseFrameRate reads ffprobe's rational form, such as "24000/1001".
func parseFrameRate(text string) float64 {
	numerator, denominator, found := strings.Cut(strings.TrimSpace(text), "/")
	top, err := strconv.ParseFloat(numerator, 64)
	if err != nil || top <= 0 {
		return 0
	}
	if !found {
		return top
	}
	bottom, err := strconv.ParseFloat(denominator, 64)
	if err != nil || bottom <= 0 {
		return 0
	}
	if rate := top / bottom; finite(rate) && rate < 1000 {
		return rate
	}
	return 0
}
