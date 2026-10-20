{ matchbox }:
# Alt+Shift+n/p must work with NumLock/CapsLock on; see the patch header.
matchbox.overrideAttrs (old: {
  patches = (old.patches or [ ]) ++ [ ./matchbox-lock-mask.patch ];
})
