# Fedora: the .rpm attached to each GitHub release.

Name:           kernel-kingdom
Version:        0.6.2
Release:        1%{?dist}
Summary:        Your computer as a pixel-art kingdom (live wallpaper / idle game)
License:        MIT
URL:            https://github.com/rsmedrano-cloud/kernel-kingdom
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  desktop-file-utils
# for the tests in %%check
BuildRequires:  python3-psutil
BuildRequires:  python3-cairo
BuildRequires:  python3-pillow

Requires:       python3-gobject
Requires:       python3-cairo
Requires:       gtk4
Recommends:     gtk4-layer-shell
Recommends:     python3-pillow
Suggests:       ffmpeg-free

%description
A pixel-art isometric world that grows from your system: processes become
realms, CPU brings storms, RAM is land, containers are fortresses. Runs as a
Wayland live wallpaper (gtk4-layer-shell) or as a window, keeps a daily
chronicle, and can show the machines of your homelab as islands.

%prep
%autosetup -n %{name}-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files reino
echo rpm > %{buildroot}%{python3_sitelib}/reino/PACKAGED
desktop-file-install --dir=%{buildroot}%{_datadir}/applications packaging/dev.kernelkingdom.Window.desktop
install -Dpm644 assets/kernel-kingdom.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/kernel-kingdom.png

%check
# shared builders are slow: looser time budgets for the performance tests
KK_PERF_SLACK=10 %{python3} -m unittest discover -s tests

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{python3_sitelib}/reino/PACKAGED
%{_bindir}/kernel-kingdom
%{_datadir}/applications/dev.kernelkingdom.Window.desktop
%{_datadir}/icons/hicolor/256x256/apps/kernel-kingdom.png

%changelog
* Wed Nov 04 2026 rsmedrano-cloud <143425037+rsmedrano-cloud@users.noreply.github.com> - 0.6.2-1
- Packages for Debian/Ubuntu, Fedora and Arch

* Wed Nov 04 2026 rsmedrano-cloud <143425037+rsmedrano-cloud@users.noreply.github.com> - 0.6.1-1
- First package
