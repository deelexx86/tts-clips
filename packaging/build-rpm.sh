#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ $(rpm --eval '%{?rhel}') != 9 || $(uname -m) != x86_64 ]]; then
    echo 'Build the RPM on Enterprise Linux 9 x86_64.' >&2
    exit 1
fi

out=$(realpath -m "${1:-output/rpm}")
version=$(/usr/bin/python3.12 -c 'from clips import __version__; print(__version__)')
name=tts-clips-$version
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
source_dir=$work/$name
mkdir -p "$work/SOURCES" "$source_dir/clips" "$source_dir/docs" "$source_dir/packaging" "$out"

npm ci
npm run build
install -m 0644 clips/*.py "$source_dir/clips/"
cp -r dist README.md "$source_dir/"
cp docs/{Editor,Server,Developer,C1.10_Release_Candidate,C1.9C_Natural_Speech_Segmentation,C0A_Source_Audit_and_Architecture_Proposal}.md "$source_dir/docs/"
mkdir -p "$source_dir/docs/fixtures/c1.9c"
cp docs/fixtures/c1.9c/riverside.txt "$source_dir/docs/fixtures/c1.9c/"
cp packaging/{setup.py,models.json,*.lock,nginx.conf.example,tts-clips.env,tts-clips.service,tts-clips-setup} "$source_dir/packaging/"
tar -czf "$work/SOURCES/$name.tar.gz" -C "$work" "$name"
rpmbuild -bb --define "_topdir $work" packaging/tts-clips.spec
rpm_path=$(find "$work/RPMS/x86_64" -name 'tts-clips-*.rpm' -print)
rpm_file=$(basename "$rpm_path")
rpm_file=${rpm_file/\~/-}
cp -- "$rpm_path" "$out/$rpm_file"
cd "$out"
sha256sum "$rpm_file" > "$rpm_file.sha256"
printf 'RPM: %s/%s\n' "$out" "$rpm_file"
