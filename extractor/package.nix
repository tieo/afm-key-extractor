{ writeShellApplication, python3, openssh, qemu, lib }:

writeShellApplication {
  name = "airtag-extract-keys";
  runtimeInputs = [ python3 openssh qemu ];
  text = builtins.readFile ./extract-keys.sh;
  meta = {
    description = "Extract AirTag keys from macOS VM";
    license = lib.licenses.mit;
  };
}
