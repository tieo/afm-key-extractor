{ writeShellApplication, curl, git, qemu, lib }:

writeShellApplication {
  name = "airtag-provision-vm";
  runtimeInputs = [ curl git qemu ];
  text = builtins.readFile ./provision-vm.sh;
  meta = {
    description = "One-time macOS VM provisioning for AirTag key extraction";
    license = lib.licenses.mit;
  };
}
