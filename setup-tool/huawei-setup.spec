# PyInstaller recipe: one file on Windows / Linux, a .app on macOS. Build from this folder:
#   pip install -r requirements.txt pyinstaller && pyinstaller huawei-setup.spec
import sys
a = Analysis(["huawei_setup.py"], pathex=[".", ".."], datas=[("web/index.html", "web")],
             hiddenimports=["adopt", "actions", "serial.tools.list_ports"], excludes=["tkinter"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="HuaweiDeviceSetup", console=True, upx=False)
if sys.platform == "darwin":
    app = BUNDLE(exe, name="HuaweiDeviceSetup.app", bundle_identifier="net.savenet.huaweisetup",
                 info_plist={"LSUIElement": False, "CFBundleShortVersionString": "1.0.0"})
