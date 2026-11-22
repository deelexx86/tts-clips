%global appdir %{_datadir}/tts-clips
%global appversion 0.1.0rc1
%global debug_package %{nil}

Name:           tts-clips
Version:        0.1.0~rc1
Release:        1%{?dist}
Summary:        Browser-based editor for Russian text-to-speech clips
License:        LicenseRef-Unspecified
URL:            https://github.com/deelexx86/tts-clips
Source0:        %{name}-%{appversion}.tar.gz
ExclusiveArch:  x86_64

BuildRequires:  python3.12
BuildRequires:  systemd-rpm-macros
Requires:       python3.12
Requires:       python3.12-pip
Requires:       git-core
Requires:       libgomp
Requires:       systemd
Requires:       util-linux
Requires(pre):  shadow-utils
%{?systemd_requires}

%description
TTS Clips provides a browser editor, a FastAPI service, persistent SQLite
storage and local VoxCPM2 and Whisper workers. After installing the RPM,
run tts-clips-setup to install the pinned Python runtime and model weights.

%prep
%autosetup -n %{name}-%{appversion}

%build
/usr/bin/python3.12 -c 'from clips import __version__; assert __version__ == "%{appversion}"'

%install
install -d %{buildroot}%{appdir} %{buildroot}%{_unitdir} %{buildroot}%{_bindir} %{buildroot}%{_sysconfdir}/tts-clips
cp -pr clips dist %{buildroot}%{appdir}/
install -pm 0644 packaging/setup.py packaging/*.lock packaging/models.json packaging/nginx.conf.example %{buildroot}%{appdir}/
install -pm 0644 packaging/tts-clips.env %{buildroot}%{_sysconfdir}/tts-clips/
install -pm 0755 packaging/tts-clips-setup %{buildroot}%{_bindir}/
/usr/bin/python3.12 - <<'PY'
import json
from pathlib import Path

models = json.loads(Path("packaging/models.json").read_text())
unit = Path("packaging/tts-clips.service").read_text()
unit = unit.replace("@VERSION@", "%{appversion}")
unit = unit.replace("@VOX_REV@", models["voxcpm2"]["revision"])
unit = unit.replace("@WHISPER_REV@", models["whisper-small"]["revision"])
Path("%{buildroot}%{_unitdir}/tts-clips.service").write_text(unit)
PY

%pre
getent group tts-clips >/dev/null || groupadd --system tts-clips
getent passwd tts-clips >/dev/null || useradd --system -g tts-clips -d /var/lib/tts-clips -s /sbin/nologin -M tts-clips

%preun
%systemd_preun tts-clips.service

%postun
%systemd_postun tts-clips.service

%files
%doc README.md docs
%{appdir}/
%{_bindir}/tts-clips-setup
%{_unitdir}/tts-clips.service
%dir %{_sysconfdir}/tts-clips
%config(noreplace) %{_sysconfdir}/tts-clips/tts-clips.env

%changelog
* Mon Nov 23 2026 TTS Clips maintainers - 0.1.0~rc1-1
- Package C1.10 for Enterprise Linux 9.
