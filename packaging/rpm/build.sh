#!/bin/sh
# Builds dist/kernel-kingdom-<version>-1.<dist>.noarch.rpm from this checkout
# (needs rpm-build, pyproject-rpm-macros, python3-devel, desktop-file-utils).
set -eu
cd "$(dirname "$0")/../.."
V=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
TOP=$(mktemp -d)
mkdir -p "$TOP/SOURCES" dist
tar --exclude=./.git --exclude=./dist --transform "s,^\.,kernel-kingdom-$V," -czf "$TOP/SOURCES/kernel-kingdom-$V.tar.gz" .
rpmbuild -bb --define "_topdir $TOP" packaging/rpm/kernel-kingdom.spec
cp "$TOP"/RPMS/noarch/kernel-kingdom-*.rpm dist/
rm -rf "$TOP"
ls dist/kernel-kingdom-*.rpm
