{ lib, python312, fetchFromGitHub, fetchurl }:

let
  py = python312.pkgs;

  # The installed server uses MCP 1.30.0; nixpkgs 26.05 has 1.26.0.
  mcp = py.buildPythonPackage {
    pname = "mcp";
    version = "1.30.0";
    format = "wheel";
    src = fetchurl {
      url = "https://files.pythonhosted.org/packages/f5/f4/e58bc33317c92a0203664daaf00bf6f41166cc0149e5d6870a03f7cd004a/mcp-1.30.0-py3-none-any.whl";
      hash = "sha256-Zm7bUAlQPhBHydYDRqdW+UsmHwXMJiXyPUHHKP/EhNA=";
    };
    propagatedBuildInputs = [
      py.anyio
      py.httpx
      py.httpx-sse
      py.jsonschema
      py.pydantic
      py.pydantic-settings
      py.pyjwt
      py.python-multipart
      py.sse-starlette
      py.starlette
      py.typing-extensions
      py.typing-inspection
      py.uvicorn
    ] ++ py.pyjwt.optional-dependencies.crypto;
    doCheck = false;
    pythonImportsCheck = [ "mcp" ];
  };

  # mijiaAPI 4.2.1 requires FastMCP >= 3.4.2. The 26.05 version is 3.2.3;
  # FastMCP 3.x ships its implementation separately as fastmcp-slim.
  pyKeyValueAio = py.buildPythonPackage {
    pname = "py-key-value-aio";
    version = "0.4.4";
    format = "wheel";
    src = fetchurl {
      url = "https://files.pythonhosted.org/packages/32/69/f1b537ee70b7def42d63124a539ed3026a11a3ffc3086947a1ca6e861868/py_key_value_aio-0.4.4-py3-none-any.whl";
      hash = "sha256-GOF1ZOyuYbmH+Qn8LNQe4gEshLSx3LjAVc+LS8G/P10=";
    };
    propagatedBuildInputs = [
      py.beartype
      py.typing-extensions
      # FastMCP requests the filetree, keyring and memory extras.
      py.aiofile
      py.anyio
      py.keyring
      py.cachetools
    ];
    doCheck = false;
    pythonImportsCheck = [ "key_value.aio" ];
  };

  fastmcpSlim = py.buildPythonPackage {
    pname = "fastmcp-slim";
    version = "3.4.2";
    format = "wheel";
    src = fetchurl {
      url = "https://files.pythonhosted.org/packages/f7/58/22afebf18df7260b09148199cbeb90cdcc4b3a4e1b5d7460e3591c3a7add/fastmcp_slim-3.4.2-py3-none-any.whl";
      hash = "sha256-vcckkiEmgcpQJ1X6iswEV/VZKV2h/D38BZmtwcBLgvM=";
    };
    propagatedBuildInputs = [
      py.platformdirs
      py.pydantic
      py.pydantic-settings
      py.python-dotenv
      py.rich
      py.typing-extensions
      # fastmcp-slim[client,server], required by the fastmcp wheel.
      py.authlib
      py.cyclopts
      py.exceptiongroup
      py.griffelib
      py.httpx
      py.joserfc
      py.jsonref
      py.jsonschema-path
      mcp
      py.openapi-pydantic
      py.opentelemetry-api
      py.packaging
      pyKeyValueAio
      py.pyperclip
      py.python-multipart
      py.pyyaml
      py.starlette
      py.uncalled-for
      py.uvicorn
      py.watchfiles
      py.websockets
    ] ++ py.pydantic.optional-dependencies.email;
    doCheck = false;
    pythonImportsCheck = [ "fastmcp" ];
  };

  fastmcp = py.buildPythonPackage {
    pname = "fastmcp";
    version = "3.4.2";
    format = "wheel";
    src = fetchurl {
      url = "https://files.pythonhosted.org/packages/58/4d/8b1ba42251160e11ca34686344572121432c23a082d56ef6bbdec5888fc1/fastmcp-3.4.2-py3-none-any.whl";
      hash = "sha256-yHpisCnwxUAK2oX2g2KTRdJGbDkWnwy4U+SHsvcwjAg=";
    };
    propagatedBuildInputs = [ fastmcpSlim ];
    doCheck = false;
    pythonImportsCheck = [ "fastmcp" ];
  };

  mijiaAPI = py.buildPythonPackage {
    pname = "mijiaAPI";
    version = "4.2.1";
    format = "wheel";
    src = fetchurl {
      url = "https://files.pythonhosted.org/packages/10/76/704d390859c22345e4437bc0ec50843ae9e6bad159709570991a4fb8c41a/mijiaapi-4.2.1-py3-none-any.whl";
      hash = "sha256-xaWubRZSeoxL9yh3mHtPx9xZsA8DshIO0K9DcbHcFNA=";
    };
    propagatedBuildInputs = [
      fastmcp
      py.pillow
      py.pycryptodome
      py.qrcode
      py.requests
      py.tzlocal
    ];
    doCheck = false;
    pythonImportsCheck = [ "mijiaAPI" ];
  };
in
py.buildPythonApplication {
  pname = "miot-mcp";
  version = "2.0.0";
  pyproject = true;

  # Matches the installed checkout; authentication, logs and QR artifacts live
  # under HOME/.miot-mcp at runtime, never in the fetched source or Nix store.
  src = fetchFromGitHub {
    owner = "javen-yan";
    repo = "miot-mcp";
    rev = "4f1cfbe549b0ab7b6faa2e27b16769ffc73c61d7";
    hash = "sha256-LvrKehxvwvSB0evSSI19EYy3ulnYrQORgsHYjmSxeV0=";
  };

  build-system = [ py.setuptools py.wheel ];
  dependencies = [ mcp mijiaAPI py.pillow py.qrcode ];

  # Upstream tests and login require a live Mijia account.
  doCheck = false;

  meta = {
    description = "Mijia smart-device MCP server";
    homepage = "https://github.com/javen-yan/miot-mcp";
    license = lib.licenses.mit;
    mainProgram = "miot-mcp";
    platforms = lib.platforms.linux;
  };
}
