{ lib, stdenvNoCC, python3, xpra, matchbox, xorg-server, src, version }:
stdenvNoCC.mkDerivation {
  pname = "tuigui";
  inherit version src;
  nativeBuildInputs = [ python3 ];
  dontBuild = true;

  installPhase = ''
    install -Dm755 src/tg.py $out/bin/tg
    install -Dm644 src/share/matchbox-kbdconfig -t $out/share/tg
    substituteInPlace $out/bin/tg \
      --replace-fail '@python3@'    '${python3}/bin/python3' \
      --replace-fail '@xpra@'       '${xpra}' \
      --replace-fail '@xorgserver@' '${xorg-server}' \
      --replace-fail '@matchbox@'   '${matchbox}' \
      --replace-fail '@share@'      "$out/share/tg" \
      --replace-fail '@version@'    '${version} (xpra ${xpra.version})'
  '';

  doCheck = true;
  # not -I here: the tests put ../../src on sys.path themselves
  checkPhase = ''python3 -m unittest discover -s tests/unit -v'';

  meta = {
    description = "Run X11 GUI programs inside the kitty terminal";
    mainProgram = "tg";
    platforms = [ "x86_64-linux" "aarch64-linux" ];
    license = lib.licenses.mit;
  };
}
