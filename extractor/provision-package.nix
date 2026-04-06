{ writeShellApplication, python3, git, qemu, lib }:

writeShellApplication {
  name = "airtag-provision-vm";
  runtimeInputs = [ python3 git qemu ];
  text = builtins.readFile ./provision-vm.sh;
  meta = {
    description = "One-time macOS VM provisioning for AirTag key extraction";
    license = lib.licenses.mit;
  };
}
