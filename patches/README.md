# SDRTrunk Patches

Patches to SDRTrunk that enable features used by sdrtrunk-pg.

---

## `sdrtrunk-v0.6.1-gps-lrrp.patch`

**What it does:** Forwards P25 LRRP GPS coordinates in the Broadcastify Calls POST.

P25 systems can broadcast unit GPS locations via LRRP (Location Registration and Reporting
Protocol). When a Motorola or L3Harris radio transmits a self-reported GPS position as part
of the P25 LC header, SDRTrunk decodes it into a `LocationIdentifier` in the call's
`IdentifierCollection` — but the stock Broadcastify Calls broadcaster never forwards that
data to the receiving server.

This patch adds two fields to the POST:
- `lat` — decimal degrees latitude
- `lon` — decimal degrees longitude

Both fields are omitted entirely if no GPS data was received for the call, so the receiving
server can treat their presence as opt-in signal.

**Applies to:** SDRTrunk v0.6.1

**Files changed:**
- `src/.../broadcastify/FormField.java` — adds `LATITUDE("lat")` and `LONGITUDE("lon")` enum values
- `src/.../broadcastify/BroadcastifyCallBroadcaster.java` — queries the IdentifierCollection for `Form.LOCATION` and appends lat/lon if present

**Status:** Applied to the running JAR at `~/git/sdr-trunk-linux-x86_64-v0.6.1/lib/sdr-trunk-0.6.1.jar`. Backup at `.jar.bak`.

**Note:** Most P25 systems do not broadcast LRRP. If your system does, coordinates will flow into `calls.lat` / `calls.lon` automatically once the patch is applied.

---

### Applying the patch to a new SDRTrunk version

You need the Bellsoft Liberica JDK (full build, includes JavaFX) to compile:

```bash
# Download Liberica JDK full (includes JavaFX)
# https://bell-sw.com/pages/downloads/ → pick your JDK version, "Full JDK"
tar -xzf bellsoft-jdk*.tar.gz -C /tmp/
export LIBERICA=/tmp/jdk-*/bin

# Clone SDRTrunk and check out the version you're patching
git clone https://github.com/DSheirer/sdrtrunk ~/git/sdrtrunk
cd ~/git/sdrtrunk
git checkout v0.6.1   # or whatever version you need

# Apply the patch
patch -p1 < /path/to/sdrtrunk-pg/patches/sdrtrunk-v0.6.1-gps-lrrp.patch

# The patch may need minor adjustment for different SDRTrunk versions.
# The logic is simple — just add the two imports and the GPS block after
# .addPart(FormField.ENCODING, ENCODING_TYPE_MP3) in BroadcastifyCallBroadcaster.java

# Compile just the two changed files against the pre-built JAR's classpath
JAR=~/git/sdr-trunk-linux-x86_64-v0.6.1/lib/sdr-trunk-0.6.1.jar
OUTDIR=/tmp/sdrtrunk-patch && mkdir -p $OUTDIR

$LIBERICA/javac \
  -source 23 -target 23 \
  -cp "$JAR:$(ls $(dirname $JAR)/*.jar | tr '\n' ':')" \
  -d $OUTDIR \
  src/main/java/io/github/dsheirer/audio/broadcast/broadcastify/FormField.java \
  src/main/java/io/github/dsheirer/audio/broadcast/broadcastify/BroadcastifyCallBroadcaster.java

# Back up the original JAR, then update it with the new class files
cp $JAR ${JAR}.bak
cd $OUTDIR
$LIBERICA/jar uf $JAR \
  io/github/dsheirer/audio/broadcast/broadcastify/FormField.class \
  io/github/dsheirer/audio/broadcast/broadcastify/BroadcastifyCallBroadcaster.class \
  "io/github/dsheirer/audio/broadcast/broadcastify/BroadcastifyCallBroadcaster\$AudioRecordingProcessor.class" \
  "io/github/dsheirer/audio/broadcast/broadcastify/BroadcastifyCallBroadcaster\$BroadcastifyCallTest.class"

echo "Done. Restart SDRTrunk."
```

> **Why Liberica?** The standard OpenJDK doesn't bundle JavaFX, which SDRTrunk
> requires. Liberica "Full JDK" includes JavaFX as part of the module system.
> You need it to compile against SDRTrunk's classpath.
