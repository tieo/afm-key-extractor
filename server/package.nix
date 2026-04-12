{ python3Packages, python3, lib, fetchPypi }:

let
  anisette = python3Packages.buildPythonPackage rec {
    pname = "anisette";
    version = "1.2.4";
    format = "wheel";

    src = fetchPypi {
      inherit pname version format;
      dist = "py3";
      python = "py3";
      hash = "sha256-9h5iCqc28MrAyhAt1EoCzCyuoaBy6rDx738ti1NOA9w=";
    };

    dependencies = with python3Packages; [
      certifi fs pyelftools typing-extensions unicorn urllib3
    ];

    pythonImportsCheck = [ "anisette" ];
  };

  findmy = python3Packages.buildPythonPackage rec {
    pname = "findmy";
    version = "0.9.8";
    format = "wheel";

    src = fetchPypi {
      inherit pname version format;
      dist = "py3";
      python = "py3";
      hash = "sha256-b6tVQbCCOiZS6MTV/KUQmxPf9//pE6NlJW5iz4H/NxQ=";
    };

    dependencies = with python3Packages; [
      srp cryptography beautifulsoup4 aiohttp bleak typing-extensions anisette
    ];

    pythonImportsCheck = [ "findmy" ];
  };

  pythonEnv = python3.withPackages (ps: [ ps.flask findmy ]);

in python3Packages.buildPythonApplication {
  pname = "airtag-tracker";
  version = "0.1.0";
  pyproject = false;

  src = ./.;

  propagatedBuildInputs = with python3Packages; [
    flask
    findmy
  ];

  installPhase = ''
    mkdir -p $out/bin $out/lib/airtag-tracker/static
    cp tracker.py $out/lib/airtag-tracker/
    cp -r static/* $out/lib/airtag-tracker/static/
    cat > $out/bin/airtag-tracker <<WRAPPER
    #!/bin/sh
    exec ${pythonEnv}/bin/python3 $out/lib/airtag-tracker/tracker.py "\$@"
    WRAPPER
    chmod +x $out/bin/airtag-tracker
  '';

  meta = {
    description = "AirTag location tracker with web UI";
    license = lib.licenses.mit;
    mainProgram = "airtag-tracker";
  };
}
