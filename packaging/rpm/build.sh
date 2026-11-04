#!/bin/sh
# Builds dist/kernel-kingdom-<version>-1.<dist>.noarch.rpm from this checkout
# (needs rpm-build, pyproject-rpm-macros, python3-devel, desktop-file-utils).
# $1: channel for the in-app updater, "rpm" (default: a file from GitHub) or "copr".
set -eu
cd "$(dirname "$0")/../.."
V=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
TOP=$(mktemp -d)
mkdir -p "$TOP/SOURCES" dist
tar --exclude=./.git --exclude=./dist --transform "s,^\.,kernel-kingdom-$V," -czf "$TOP/SOURCES/kernel-kingdom-$V.tar.gz" .
rpmbuild -bb --define "_topdir $TOP" --define "kk_channel ${1:-rpm}" packaging/rpm/kernel-kingdom.spec
cp "$TOP"/RPMS/noarch/kernel-kingdom-*.rpm dist/
rm -rf "$TOP"
ls dist/kernel-kingdom-*.rpm
