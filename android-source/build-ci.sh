#!/bin/bash
# Builds ServerAssistant.apk on a GitHub runner (Android SDK preinstalled, no Gradle).
# Signing key: the file named in $KEYSTORE (the stable key, so the app updates in place on the phone).
# Without one a throw-away key is made, which is fine for a test build but cannot update an installed app.
set -e
cd "$(dirname "$0")"
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-/usr/local/lib/android/sdk}}"
BT="$SDK/build-tools/$(ls "$SDK/build-tools" | sort -V | tail -1)"
JAR="$SDK/platforms/$(ls "$SDK/platforms" | grep -E '^android-[0-9]+$' | sort -V | tail -1)/android.jar"
VER=$(tr -d '[:space:]' < ../VERSION)
IFS=. read -r MA MI PA _ <<< "$VER"
CODE=$(( ${MA:-1} * 10000 + ${MI:-0} * 100 + ${PA:-0} ))
echo "Building $VER (code $CODE) with $BT and $JAR"
rm -rf build && mkdir -p build/gen build/classes build/dex
# aapt only adds --version-code/--version-name when the manifest has none, so write them into a copy of it
sed -e "s/android:versionCode=\"[^\"]*\"/android:versionCode=\"$CODE\"/" \
    -e "s/android:versionName=\"[^\"]*\"/android:versionName=\"$VER\"/" AndroidManifest.xml > build/AndroidManifest.xml
"$BT/aapt" package -f -m -J build/gen -M build/AndroidManifest.xml -S res -I "$JAR" --version-code "$CODE" --version-name "$VER" -F build/unsigned.apk
javac -source 8 -target 8 -Xlint:-options -bootclasspath "$JAR" -classpath "$JAR" -d build/classes $(find src build/gen -name '*.java')
"$BT/d8" --min-api 21 --lib "$JAR" --output build/dex $(find build/classes -name '*.class')
(cd build/dex && "$BT/aapt" add ../unsigned.apk classes.dex >/dev/null)
# the private-network helper (a static 64-bit ARM program) rides inside the app as a native library
if [ -n "${HELPER:-}" ] && [ -f "$HELPER" ]; then
  mkdir -p build/pkg/lib/arm64-v8a && cp "$HELPER" build/pkg/lib/arm64-v8a/libsatailnet.so
  (cd build/pkg && "$BT/aapt" add ../unsigned.apk lib/arm64-v8a/libsatailnet.so >/dev/null)
  echo "Added the helper ($(du -h "$HELPER" | cut -f1))"
else
  echo "No HELPER given: the app is built without the away-from-home connection"
fi
"$BT/zipalign" -f 4 build/unsigned.apk build/aligned.apk
KS="${KEYSTORE:-}"
if [ -z "$KS" ] || [ ! -f "$KS" ]; then
  echo "No signing key given: using a throw-away key (this build cannot update an installed app)"
  KS=build/temp.keystore
  keytool -genkeypair -keystore "$KS" -alias sa -keyalg RSA -keysize 2048 -validity 10000 \
      -storepass serverassistant -keypass serverassistant -dname "CN=Server Assistant" >/dev/null 2>&1
fi
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:serverassistant --key-pass pass:serverassistant --out ServerAssistant.apk build/aligned.apk
"$BT/apksigner" verify --print-certs ServerAssistant.apk | head -2
ls -l ServerAssistant.apk
