#!/bin/bash
# Builds ServerAssistant.apk with plain Android tools (no Gradle).
set -e
cd "$(dirname "$0")"
SDK=/usr/lib/android-sdk
JAR=$SDK/platforms/android-23/android.jar
rm -rf build && mkdir -p build/gen build/classes build/dex
aapt package -f -m -J build/gen -M AndroidManifest.xml -S res -I $JAR -F build/unsigned.apk
javac -source 8 -target 8 -Xlint:-options -bootclasspath $JAR -classpath $JAR -d build/classes \
      $(find src build/gen -name '*.java')
java -jar $SDK/build-tools/debian/lib/dx.jar --dex --min-sdk-version=21 --output=build/dex/classes.dex build/classes
(cd build/dex && aapt add ../unsigned.apk classes.dex >/dev/null)
zipalign -f 4 build/unsigned.apk build/aligned.apk
[ -f release.keystore ] || keytool -genkeypair -keystore release.keystore -alias sa -keyalg RSA -keysize 2048 \
      -validity 10000 -storepass serverassistant -keypass serverassistant -dname "CN=Server Assistant" >/dev/null 2>&1
apksigner sign --ks release.keystore --ks-pass pass:serverassistant --key-pass pass:serverassistant \
      --out ServerAssistant.apk build/aligned.apk
apksigner verify --print-certs ServerAssistant.apk | head -2
