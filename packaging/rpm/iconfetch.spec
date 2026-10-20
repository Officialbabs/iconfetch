Name:           iconfetch
Version:        1.3.0
Release:        1
Summary:        Give installed apps their real icons
License:        GPL-3.0-or-later
URL:            https://github.com/Officialbabs/iconfetch
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz
BuildArch:      noarch

Requires:       python3 >= 3.9
Requires:       python3-gobject
# The cairo typelib GTK's bindings need; PyGObject no longer pulls it in.
Requires:       gobject-introspection
Requires:       gtk4 >= 4.6
Requires:       libadwaita >= 1.1
Requires:       gdk-pixbuf2
Requires:       librsvg2
Requires:       shared-mime-info

%description
IconFetch lists installed apps with the icon each one shows now, finds its
real icon online (Flathub, the Snap Store, the app's website, brand icons or
the Papirus theme) and applies it at once. Every change can be undone.

%prep
%autosetup -n %{name}-%{version}

%build

%install
sh install.sh --prefix=%{_prefix} --destdir=%{buildroot} --no-deps

%files
%license LICENSE
%doc README.md
%{_bindir}/iconfetch
%{_prefix}/lib/iconfetch/
%{_datadir}/applications/io.iconfetch.IconFetch.desktop
%{_datadir}/icons/hicolor/scalable/apps/io.iconfetch.IconFetch.svg
%{_datadir}/metainfo/io.iconfetch.IconFetch.metainfo.xml
%{_prefix}/lib/systemd/user/iconfetch-refresh.path
%{_prefix}/lib/systemd/user/iconfetch-refresh.service
%config(noreplace) %{_sysconfdir}/xdg/autostart/io.iconfetch.IconFetch.Refresh.desktop

%changelog
* Tue Oct 20 2026 Oluwasegun Babalola <133790822+Officialbabs@users.noreply.github.com> - 1.3.0-1
- Faster: shows apps at once, remembers finds for a week, quicker first search
* Tue Oct 20 2026 Oluwasegun Babalola <133790822+Officialbabs@users.noreply.github.com> - 1.2.0-1
- Copies follow app updates and are removed with uninstalled apps
* Tue Oct 20 2026 Oluwasegun Babalola <133790822+Officialbabs@users.noreply.github.com> - 1.1.0-1
- Installs on most distributions; .deb, .rpm and Arch packages
