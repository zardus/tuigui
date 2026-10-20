{ xpra, xpra-src }:
(xpra.override { withHtml = false; }).overrideAttrs (old: {
  version = "8.0-unstable-${builtins.substring 0 8 xpra-src.lastModifiedDate}-${xpra-src.shortRev}";
  src = xpra-src;
  # nixpkgs' fix-41106.patch does not apply to master (runner_script.py changed);
  # fix-122159.patch applies only with fuzz and concerns systemd-run, which tg disables.
  # Set explicitly so a nixpkgs bump cannot inject patches that no longer apply.
  patches = [ ];
  doInstallCheck = false; # the only installCheck content is udevCheckHook (udev rules verify), irrelevant for tg
  # nix-update would bump the overridden version; the changelog link names 6.5.x
  passthru = (removeAttrs (old.passthru or { }) [ "updateScript" ]) // { inherit xpra-src; };
  meta = old.meta // { changelog = "https://github.com/Xpra-org/xpra/commits/${xpra-src.rev}"; };
})
