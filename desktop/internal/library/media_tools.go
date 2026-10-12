package library

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/managedtools"
)

type commandMediaTools struct {
	dataDirectory string
}

func (tools commandMediaTools) Probe(ctx context.Context, path string) (Metadata, error) {
	executable, err := managedtools.Find(tools.dataDirectory, "ffprobe")
	if err != nil {
		return Metadata{}, errors.New("ffprobe is required to import media")
	}
	probeContext, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	command := exec.CommandContext(probeContext, executable,
		"-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height:stream_disposition=attached_pic",
		"-of", "json", path,
	)
	configureMediaCommand(command)
	output, err := command.Output()
	if err != nil {
		return Metadata{}, fmt.Errorf("probe media: %w", err)
	}
	var value struct {
		Format struct {
			Duration string `json:"duration"`
		} `json:"format"`
		Streams []struct {
			CodecType   string `json:"codec_type"`
			Width       int    `json:"width"`
			Height      int    `json:"height"`
			Disposition struct {
				AttachedPic int `json:"attached_pic"`
			} `json:"disposition"`
		} `json:"streams"`
	}
	if err := json.Unmarshal(output, &value); err != nil {
		return Metadata{}, fmt.Errorf("decode ffprobe output: %w", err)
	}
	duration, _ := strconv.ParseFloat(value.Format.Duration, 64)
	metadata := Metadata{Duration: duration}
	for _, stream := range value.Streams {
		switch strings.ToLower(stream.CodecType) {
		case "video":
			if stream.Disposition.AttachedPic != 0 {
				metadata.HasCover = true
				continue
			}
			metadata.HasVideo = true
			if metadata.Width == 0 {
				metadata.Width, metadata.Height = stream.Width, stream.Height
			}
		case "audio":
			metadata.HasAudio = true
		}
	}
	return metadata, nil
}

func (tools commandMediaTools) Generate(ctx context.Context, input, output string, duration float64) error {
	executable, err := managedtools.Find(tools.dataDirectory, "ffmpeg")
	if err != nil {
		return errors.New("ffmpeg is required to generate thumbnails")
	}
	thumbnailContext, cancel := context.WithTimeout(ctx, time.Minute)
	defer cancel()
	args := []string{"-v", "error"}
	// A zero duration means an audio source: its only picture, if any, is the
	// cover art, which sits at the start and is not reachable by seeking.
	if duration > 0 {
		at := 1.0
		if duration > 10 {
			at = min(duration*0.1, 60)
		}
		args = append(args, "-ss", strconv.FormatFloat(at, 'f', -1, 64))
	}
	args = append(args, "-i", input, "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", "-y", output)
	command := exec.CommandContext(thumbnailContext, executable, args...)
	configureMediaCommand(command)
	if data, err := command.CombinedOutput(); err != nil {
		return fmt.Errorf("generate thumbnail: %s", strings.TrimSpace(string(data)))
	}
	if _, err := os.Stat(output); err != nil {
		return err
	}
	return nil
}
