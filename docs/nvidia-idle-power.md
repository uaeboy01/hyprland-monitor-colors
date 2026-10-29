# RTX 5080 / Linux idle-power observations

These observations describe one CachyOS system, not a universal recommendation.
The Monitor Colors application does not set NVIDIA parameters, clocks, power
limits, or display refresh rates.

## Tested arrangement

- NVIDIA RTX 5080, driver and open kernel modules 615.78.08.
- CachyOS LTS kernel 6.18.55-2, Hyprland 0.56.2, Wayland, Noctalia.
- Primary 3840×2160 at 160 Hz over DisplayPort; secondary 4K at about 60 Hz.
- OBS Flatpak replay buffer: 4K capture, 1080p60 H.264 NVENC output,
  lookahead and multipass processing enabled.
- PowerMizer Adaptive; VRR inactive in the supplied monitor dump.

## Before and after

Before the experiment, primary 144/160 Hz operation was reported to hold memory
around 15001 MHz even with little desktop activity. At 120 Hz it could reach
810 MHz. Reported desktop temperatures were generally in the mid-40s C.

The user enabled the documented driver token:

```text
NVreg_RegistryDwords=RmDisableDisplayGlitchPerfLimit=1
```

After reboot, a user-supplied 160 Hz sample showed memory reaching 810 MHz,
frequent 7001 MHz intervals, and occasional 14801 MHz boosts. Quiet intervals
were approximately 25–28 W with temperatures of 34–36 C. Brief higher-power
samples remained. OBS capture continued to be part of the workload.

This supports a display-related performance restriction as a major contributor.
It does not identify the driver's exact internal constraint or prove that all
boosts are avoidable. Longer-term stability and other driver versions were not
established by this sample.

## Timings and interpretation

The primary EDID advertised approximately 71.5 microseconds of vertical blanking
at 160 Hz, 172.4 at 144 Hz, and 206.9 at 120 Hz. That difference supports
investigating display transition constraints, but the 144 Hz result means the
shortest blanking interval alone is not a complete explanation.

Actual negotiated DisplayPort link rate, DSC state, and wire color depth were
not captured. Do not present DSC as the confirmed root cause.

## Documented tradeoff

NVIDIA says this token can reduce idle power in some multi-monitor configurations
by allowing memory-clock changes while display output uses memory. Momentary
display glitches can result. Watch for flicker, corruption, black screens, or
signal loss during ordinary use. Revert the option if it causes problems.

The original system used a module-option file under `/etc/modprobe.d/`, followed
by CachyOS's `limine-mkinitcpio` regeneration and a planned reboot. That is
system-specific administration, not an installation step for this app. The
repository intentionally includes no driver-tuning script.

## Sources

- [NVIDIA 615 release notes](https://forums.developer.nvidia.com/t/615-release-feedback-discussion/382815)
- [NVIDIA RTX 5080 specifications](https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5080/)
- [CachyOS boot-manager configuration](https://wiki.cachyos.org/configuration/boot_manager_configuration/)
