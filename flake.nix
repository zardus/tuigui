{
  description = "tuigui (tg): run X11 GUI programs inside the kitty terminal via xpra";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable"; # pinned by flake.lock
    xpra-src = {
      # only master has the terminal backend; pinned by flake.lock
      url = "github:Xpra-org/xpra";
      flake = false;
    };
  };

  outputs = { self, nixpkgs, xpra-src }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      version = "0.1.0";
    in {
      packages = forAll (pkgs: rec {
        xpra = pkgs.callPackage ./nix/xpra.nix { inherit xpra-src; };
        matchbox = pkgs.callPackage ./nix/matchbox.nix { };
        tg = pkgs.callPackage ./nix/tg.nix { inherit xpra matchbox version; src = self; };
        default = tg;
      });

      apps = forAll (pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.tg}/bin/tg";
          meta.description = "Run an X11 GUI program inside the kitty terminal";
        };
      });

      checks = forAll (pkgs:
        let p = self.packages.${pkgs.stdenv.hostPlatform.system}; in {
          tg = p.tg; # builds and runs the unit tests (checkPhase)
          cli = pkgs.runCommand "tg-cli-check" { } ''
            tg=${p.tg}/bin/tg
            $tg --version | grep -q '^tg ${version} (xpra '
            $tg --help | grep -q '^usage: tg'
            $tg </dev/null 2>&1 | grep -q "no running xpra servers"          # bare tg lists
            rc=0; $tg -s </dev/null >/dev/null 2>&1 || rc=$?; [ "$rc" = 2 ]     # no COMMAND
            rc=0; $tg true </dev/null >/dev/null 2>err || rc=$?; [ "$rc" = 2 ]  # no tty
            grep -q 'must be the kitty terminal' err
            touch $out
          '';
          lint = pkgs.runCommand "tg-lint" { nativeBuildInputs = [ pkgs.ruff ]; } ''
            ruff check --no-cache ${self}/src/tg.py ${self}/tests && touch $out
          '';
        });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            self.packages.${pkgs.stdenv.hostPlatform.system}.xpra
            pkgs.ruff pkgs.xdotool pkgs.imagemagick pkgs.xterm pkgs.xrandr pkgs.xwininfo
          ];
        };
      });
    };
}
