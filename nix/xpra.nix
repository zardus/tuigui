{ xpra, xpra-src }:
(xpra.override { withHtml = false; }).overrideAttrs (old: {
  version = "8.0-unstable-${builtins.substring 0 8 xpra-src.lastModifiedDate}-${xpra-src.shortRev}";
  src = xpra-src;
  # nixpkgs' fix-41106.patch does not apply to master (runner_script.py changed);
  # fix-122159.patch applies only with fuzz and concerns systemd-run, which tg disables.
  # Set explicitly so a nixpkgs bump cannot inject patches that no longer apply.
  patches = [ ];
  doInstallCheck = false; # installCheck was written for 6.5.x
  passthru = (old.passthru or { }) // { inherit xpra-src; };
})
