package library

import (
	"strings"
	"testing"
)

func encoderNames(encoders []exportEncoder) string {
	names := make([]string, len(encoders))
	for index, encoder := range encoders {
		names[index] = encoder.name
	}
	return strings.Join(names, ",")
}

func TestExportEncodersOrder(t *testing.T) {
	service := &Service{}
	if got := encoderNames(service.exportEncoders(false)); got != "software" {
		t.Fatalf("cpu encoders = %s", got)
	}
	if got := encoderNames(service.exportEncoders(true)); got != "nvidia,intel,amd,software" {
		t.Fatalf("default gpu encoders = %s", got)
	}
	// A proxy built on Quick Sync hints at the vendor before any export ran.
	service.proxyPipeline = "intel"
	if got := encoderNames(service.exportEncoders(true)); got != "intel,nvidia,amd,software" {
		t.Fatalf("proxy-hinted encoders = %s", got)
	}
	service.rememberExportEncoder(hardwareExportEncoders[2])
	if got := encoderNames(service.exportEncoders(true)); got != "amd,nvidia,intel,software" {
		t.Fatalf("remembered encoders = %s", got)
	}
	// Falling back to the CPU says nothing about which card works.
	service.rememberExportEncoder(softwareExportEncoder)
	if service.exportEncoder != "amd" {
		t.Fatalf("software export overwrote the hardware preference: %q", service.exportEncoder)
	}
}

func TestBuildEditorExportArgsHardwareEncoders(t *testing.T) {
	for name, want := range map[string]string{
		"nvidia": "-sn -dn -c:v h264_nvenc -preset p6 -tune hq -rc vbr -cq 18 -b:v 0 -spatial-aq 1 -pix_fmt yuv420p -c:a aac",
		"intel":  "-sn -dn -c:v h264_qsv -preset slow -global_quality 18 -pix_fmt nv12 -c:a aac",
		"amd":    "-sn -dn -c:v h264_amf -quality quality -rc cqp -qp_i 18 -qp_p 20 -pix_fmt nv12 -c:a aac",
	} {
		var encoder exportEncoder
		for _, candidate := range hardwareExportEncoders {
			if candidate.name == name {
				encoder = candidate
			}
		}
		args := strings.Join(buildEditorExportArgs(editorExportJob{
			video: "source.mp4", audio: "source.mp4", pieces: []Range{{T0: 0, T1: 5}},
			filter: "subtitles=subtitle.ass", crf: 18, preset: "slow", encoder: encoder,
			audioArgs: []string{"-c:a", "aac", "-b:a", "192k"}, container: exportContainers["mp4"], output: "out.part.mp4",
		}), " ")
		if !strings.Contains(args, want) || strings.Contains(args, "libx264") {
			t.Fatalf("%s args = %s", name, args)
		}
	}
}

func TestHardwarePresetMapping(t *testing.T) {
	for preset, want := range map[string][3]string{
		"ultrafast": {"p1", "veryfast", "speed"},
		"veryfast":  {"p2", "veryfast", "speed"},
		"medium":    {"p5", "medium", "balanced"},
		"veryslow":  {"p7", "veryslow", "quality"},
	} {
		got := [3]string{nvencPreset(preset), qsvPreset(preset), amfQuality(preset)}
		if got != want {
			t.Fatalf("%s maps to %v; want %v", preset, got, want)
		}
	}
}
