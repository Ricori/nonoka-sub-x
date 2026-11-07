package library

import "strconv"

// exportEncoder is one H.264 encoder the editor export can burn subtitles
// with. Unlike the proxy pipelines, frames stay in system memory throughout:
// the subtitles filter (libass) only runs on the CPU, so the hardware encoders
// take the filtered frames from there instead of keeping them on the GPU.
type exportEncoder struct {
	name  string
	label string
	// hardware encoders spend more bits than libx264 at the same quality
	// setting; the export window warns about it before they are chosen.
	hardware bool
	args     func(crf int, preset string) []string
}

var softwareExportEncoder = exportEncoder{
	name: "software", label: "CPU · x264",
	args: func(crf int, preset string) []string {
		return []string{"-c:v", "libx264", "-preset", preset, "-crf", strconv.Itoa(crf)}
	},
}

// hardwareExportEncoders map the x264 knobs onto each vendor's own: CRF onto
// the constant-quality mode the encoder has, the x264 preset onto its
// speed/quality ladder. The names match proxyPipelines so a vendor that already
// built a proxy on this machine is tried first.
var hardwareExportEncoders = []exportEncoder{
	{
		name: "nvidia", label: "NVIDIA NVENC", hardware: true,
		args: func(crf int, preset string) []string {
			return []string{"-c:v", "h264_nvenc", "-preset", nvencPreset(preset), "-tune", "hq",
				"-rc", "vbr", "-cq", strconv.Itoa(crf), "-b:v", "0", "-spatial-aq", "1",
				"-pix_fmt", "yuv420p"}
		},
	},
	{
		name: "intel", label: "Intel Quick Sync", hardware: true,
		args: func(crf int, preset string) []string {
			return []string{"-c:v", "h264_qsv", "-preset", qsvPreset(preset),
				"-global_quality", strconv.Itoa(crf), "-pix_fmt", "nv12"}
		},
	},
	{
		name: "amd", label: "AMD AMF", hardware: true,
		args: func(crf int, preset string) []string {
			qp := strconv.Itoa(crf)
			return []string{"-c:v", "h264_amf", "-quality", amfQuality(preset), "-rc", "cqp",
				"-qp_i", qp, "-qp_p", strconv.Itoa(min(51, crf+2)), "-pix_fmt", "nv12"}
		},
	},
}

func nvencPreset(preset string) string {
	switch preset {
	case "ultrafast", "superfast":
		return "p1"
	case "veryfast":
		return "p2"
	case "faster":
		return "p3"
	case "fast":
		return "p4"
	case "slow":
		return "p6"
	case "slower", "veryslow":
		return "p7"
	}
	return "p5"
}

// qsvPreset: QSV has every x264 name from veryfast to veryslow.
func qsvPreset(preset string) string {
	if preset == "ultrafast" || preset == "superfast" {
		return "veryfast"
	}
	return preset
}

func amfQuality(preset string) string {
	switch preset {
	case "ultrafast", "superfast", "veryfast", "faster":
		return "speed"
	case "slow", "slower", "veryslow":
		return "quality"
	}
	return "balanced"
}

// exportEncoders lists what to try in order. With gpu the hardware encoders
// come first, the one that last worked (for an export, else for a proxy)
// leading, and libx264 always closes the list so a missing card or driver
// only costs the failed attempts.
func (s *Service) exportEncoders(gpu bool) []exportEncoder {
	if !gpu {
		return []exportEncoder{softwareExportEncoder}
	}
	s.mu.RLock()
	preferred := s.exportEncoder
	if preferred == "" {
		preferred = s.proxyPipeline
	}
	s.mu.RUnlock()
	ordered := make([]exportEncoder, 0, len(hardwareExportEncoders)+1)
	for _, encoder := range hardwareExportEncoders {
		if encoder.name == preferred {
			ordered = append(ordered, encoder)
		}
	}
	for _, encoder := range hardwareExportEncoders {
		if encoder.name != preferred {
			ordered = append(ordered, encoder)
		}
	}
	return append(ordered, softwareExportEncoder)
}

func (s *Service) rememberExportEncoder(encoder exportEncoder) {
	if !encoder.hardware {
		return
	}
	s.mu.Lock()
	s.exportEncoder = encoder.name
	s.mu.Unlock()
}
