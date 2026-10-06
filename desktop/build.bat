@echo off
REM ==========================================================================
REM  Aegis 一键构建脚本（Windows / Python 3.10）
REM
REM  用法：在仓库根目录 Aiagent\ 下执行
REM            desktop\build.bat
REM
REM  流程： 建 venv - 装依赖 - 密钥扫描 - PyInstaller 打包 - 二次密钥扫描
REM  产物： dist\Aegis\Aegis.exe
REM
REM  ⚠️ 本文件必须保存为「UTF-8 无 BOM」，否则中文提示会乱码。
REM     脚本第一行已切到 65001 代码页，控制台请使用 TrueType 字体（如 Consolas）。
REM ==========================================================================
chcp 65001 >nul
setlocal enabledelayedexpansion

set "ROOT=%~dp0.."
pushd "%ROOT%" 2>nul
if errorlevel 1 (
    echo [失败] 无法进入仓库根目录：%ROOT%
    exit /b 1
)
set "ROOT=%CD%"
set "VENV=%ROOT%\.venv-build"
set "PYEXE=%VENV%\Scripts\python.exe"
set "PIPEXE=%VENV%\Scripts\pip.exe"
set "PYIEXE=%VENV%\Scripts\pyinstaller.exe"
REM  版本号需与 desktop\app.py 的 __version__、installer.iss 的 MyAppVersion 保持一致
set "VERSION=1.8.0"
set "PATTERN=%TEMP%\aegis-scan-patterns.txt"
set "HITS=%TEMP%\aegis-scan-hits.txt"
set "HIT=0"

echo.
echo ============================================================
echo  Aegis 构建脚本
echo  仓库根目录：%ROOT%
echo ============================================================
echo.

REM ── 步骤 0：检查 Python 版本 ──────────────────────────────
echo [0/5] 检查 Python 版本
where python >nul 2>nul
if errorlevel 1 (
    echo [失败] 未在 PATH 中找到 python，请先安装 Python 3.10.x 并勾选 Add to PATH。
    exit /b 1
)
python -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)"
if errorlevel 1 (
    echo [失败] 需要 Python 3.10 或更高版本（推荐 3.10.x）。
    python --version
    exit /b 1
)
python --version
echo.

REM ── 步骤 1：创建 / 重建构建用虚拟环境 ────────────────────
echo [1/5] 创建构建虚拟环境（%VENV%），预计 30 秒 ~ 1 分钟
if exist "%VENV%" (
    echo       已存在旧的构建环境，先清理以保证产物纯净
    rmdir /s /q "%VENV%" 2>nul
)
python -m venv "%VENV%"
if errorlevel 1 (
    echo [失败] 创建虚拟环境失败，请确认已安装 python -m venv 组件。
    exit /b 1
)
"%PYEXE%" -m pip install --upgrade pip -q
if errorlevel 1 (
    echo [失败] 升级 pip 失败，请检查网络或镜像源配置。
    exit /b 1
)
echo       虚拟环境就绪
echo.

REM ── 步骤 2：安装依赖 ─────────────────────────────────────
echo [2/5] 安装依赖（requirements.txt + desktop\requirements-desktop.txt）
echo       预计 2 ~ 5 分钟，取决于网络状况
"%PIPEXE%" install -r "%ROOT%\requirements.txt" -r "%ROOT%\desktop\requirements-desktop.txt"
if errorlevel 1 (
    echo [失败] 依赖安装失败，请检查网络或镜像源配置。
    exit /b 1
)
echo       依赖安装完成
echo.

REM ── 步骤 3：构建前密钥扫描 ───────────────────────────────
REM  扫描对象：apps / static / templates / config / main.py / desktop 的源码类文件。
REM  不扫 .bat 与 .md：一个是构建脚本自身、一个是文档，都不进安装包，
REM  且脚本内必须写出被扫描的特征串，扫自己必然误报。
echo [3/5] 构建前密钥扫描（源码）
REM  三条特征（写入临时模式文件，避免命令行里的引号被 cmd 吃掉）：
REM    1. 以 sk- 开头的常见密钥前缀（词首匹配，不会误伤 task- 之类的普通单词）
REM    2. 代码里 api_key 紧跟等号并直接赋字符串字面量
REM    3. JSON 里 "api_key" 字段被赋非空字符串字面量（空串不算，避免误报）
> "%PATTERN%" echo ^\<sk-[A-Za-z0-9_-]*
>> "%PATTERN%" echo api_key[ ]*[:=][ ]*["'][A-Za-z0-9_-][A-Za-z0-9_-]*["']
>> "%PATTERN%" echo "api_key"[ ]*:[ ]*["'][A-Za-z0-9_-][A-Za-z0-9_-]*["']

if exist "%HITS%" del /q "%HITS%" 2>nul
for %%D in ("%ROOT%\apps" "%ROOT%\static" "%ROOT%\templates" "%ROOT%\config" "%ROOT%\desktop") do (
    findstr /S /I /M /R /G:"%PATTERN%" "%%~D\*.py" "%%~D\*.js" "%%~D\*.html" "%%~D\*.css" "%%~D\*.json" "%%~D\*.txt" "%%~D\*.iss" "%%~D\*.spec" "%%~D\*.cfg" "%%~D\*.ini" "%%~D\*.toml" "%%~D\*.yml" "%%~D\*.yaml" >> "%HITS%" 2>nul
    if not errorlevel 1 set "HIT=1"
)
findstr /I /M /R /G:"%PATTERN%" "%ROOT%\main.py" "%ROOT%\.env" >> "%HITS%" 2>nul
if not errorlevel 1 set "HIT=1"

if "%HIT%"=="1" (
    echo.
    echo [失败] 密钥扫描命中：源码中存在疑似硬编码密钥，构建已中止。
    echo        命中文件如下，请改为「设置面板填写」或「系统密钥库」，切勿入库：
    type "%HITS%"
    echo.
    echo        参照 docs\README.md 与 desktop\README-打包与分发.md 的密钥处理约定。
    exit /b 1
)
echo       未发现硬编码密钥，继续构建
echo.

REM ── 步骤 4：PyInstaller 打包 ─────────────────────────────
echo [4/5] PyInstaller 打包（onedir + GUI 模式），预计 2 ~ 6 分钟
if exist "%ROOT%\dist\Aegis" (
    echo       清理上一次的产物
    rmdir /s /q "%ROOT%\dist\Aegis" 2>nul
)
"%PYIEXE%" --noconfirm "%ROOT%\desktop\build.spec"
if errorlevel 1 (
    echo [失败] PyInstaller 打包失败，请向上滚动查看具体报错。
    exit /b 1
)
if not exist "%ROOT%\dist\Aegis\Aegis.exe" (
    echo [失败] 未找到产物 dist\Aegis\Aegis.exe，打包异常结束。
    exit /b 1
)
echo       产物已生成：%ROOT%\dist\Aegis\Aegis.exe
echo.

REM ── 步骤 5：产物二次密钥扫描 ─────────────────────────────
echo [5/5] 产物二次密钥扫描（dist\Aegis）
if exist "%HITS%" del /q "%HITS%" 2>nul
set "HIT=0"
findstr /S /I /M /R /G:"%PATTERN%" "%ROOT%\dist\Aegis\*.py" "%ROOT%\dist\Aegis\*.js" "%ROOT%\dist\Aegis\*.html" "%ROOT%\dist\Aegis\*.json" "%ROOT%\dist\Aegis\*.txt" >> "%HITS%" 2>nul
if not errorlevel 1 set "HIT=1"

if "%HIT%"=="1" (
    echo.
    echo [失败] 产物中含有疑似硬编码密钥，禁止分发，构建已中止。
    echo        命中文件如下：
    type "%HITS%"
    echo.
    echo        请先清理 dist\Aegis 并修复源码后重新构建。
    exit /b 1
)
echo       产物密钥扫描通过
echo.

REM ── 收尾 ─────────────────────────────────────────────────
echo ============================================================
echo  构建成功
echo  产物目录： %ROOT%\dist\Aegis
echo  主程序：   %ROOT%\dist\Aegis\Aegis.exe
echo.
echo  版本号：   %VERSION%
echo  下一步：用 Inno Setup 6 编译 desktop\installer.iss，
echo        生成最终安装包 Output\Aegis-Setup-%VERSION%.exe
echo        详细步骤见 desktop\README-打包与分发.md
echo ============================================================
echo.
popd
endlocal
exit /b 0
