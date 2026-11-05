#!/bin/sh
set -eu
python - <<'PY'
import hashlib
import io
import urllib.request
import zipfile

url = "https://software.verapdf.org/releases/1.30/verapdf-greenfield-1.30.3-installer.zip"
data = urllib.request.urlopen(url, timeout=60).read()
assert hashlib.sha256(data).hexdigest() == "b512bf24945503a33fc10738ecffc1f2dccaaf8593a448e94ee9ac86a9559a59"
zipfile.ZipFile(io.BytesIO(data)).extractall("/tmp/verapdf-installer")
PY
cat > /tmp/verapdf-install.xml <<'XML'
<AutomatedInstallation langpack="eng">
  <com.izforge.izpack.panels.htmlhello.HTMLHelloPanel id="welcome"/>
  <com.izforge.izpack.panels.target.TargetPanel id="install_dir"><installpath>/opt/verapdf</installpath></com.izforge.izpack.panels.target.TargetPanel>
  <com.izforge.izpack.panels.packs.PacksPanel id="sdk_pack_select">
    <pack index="0" name="veraPDF GUI" selected="false"/>
    <pack index="1" name="veraPDF CLI" selected="true"/>
    <pack index="2" name="veraPDF Documentation" selected="false"/>
    <pack index="3" name="veraPDF Sample Plugins" selected="false"/>
  </com.izforge.izpack.panels.packs.PacksPanel>
  <com.izforge.izpack.panels.install.InstallPanel id="install"/>
  <com.izforge.izpack.panels.finish.FinishPanel id="finish"/>
</AutomatedInstallation>
XML
java -jar /tmp/verapdf-installer/verapdf-greenfield-1.30.3/verapdf-izpack-installer-1.30.3.jar /tmp/verapdf-install.xml
ln -s /opt/verapdf/verapdf /usr/local/bin/verapdf
rm -rf /tmp/verapdf-installer /tmp/verapdf-install.xml
