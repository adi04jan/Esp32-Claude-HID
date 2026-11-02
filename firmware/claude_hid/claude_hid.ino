// ABKey HID: BLE keyboard + absolute mouse, driven by line commands over USB serial.
// The firmware is a dumb report forwarder; all key maps and scaling live on the host.
//
// Protocol (one reply line per command, decimal numbers):
//   K <mod> <k1> <k2> <k3> <k4> <k5> <k6>   keyboard report            -> OK
//   M <buttons> <x> <y> <wheel>              absolute mouse report      -> OK   (x,y 0..32767)
//   S                                        status -> OK connected=<0|1> paused=<0|1> bonded=<0|1>
//   R                                        release everything (always allowed) -> OK
//   B                                        forget the bonded PC, disconnect    -> OK
// Errors: ERR paused | ERR not connected | ERR bad command
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#include <Preferences.h>

static const int KILL_PIN = 9;          // kill switch, active low
static const int LED_PIN = 8;           // onboard LED, active low
static const uint32_t GUARD_MS = 200;   // below Windows' key-repeat delay

static const uint8_t REPORT_MAP[] = {
  // Report 1: keyboard (modifiers, reserved, 6 keys)
  0x05,0x01,0x09,0x06,0xA1,0x01,0x85,0x01, 0x05,0x07,0x19,0xE0,0x29,0xE7,0x15,0x00,0x25,0x01,0x75,0x01,0x95,0x08,0x81,0x02,
  0x95,0x01,0x75,0x08,0x81,0x01, 0x95,0x06,0x75,0x08,0x15,0x00,0x25,0x73,0x05,0x07,0x19,0x00,0x29,0x73,0x81,0x00, 0xC0,
  // Report 2: absolute mouse (3 buttons, X/Y 0..32767, relative wheel)
  0x05,0x01,0x09,0x02,0xA1,0x01,0x85,0x02, 0x09,0x01,0xA1,0x00,
    0x05,0x09,0x19,0x01,0x29,0x03,0x15,0x00,0x25,0x01,0x95,0x03,0x75,0x01,0x81,0x02, 0x95,0x01,0x75,0x05,0x81,0x01,
    0x05,0x01,0x09,0x30,0x09,0x31,0x16,0x00,0x00,0x26,0xFF,0x7F,0x75,0x10,0x95,0x02,0x81,0x02,
    0x09,0x38,0x15,0x81,0x25,0x7F,0x75,0x08,0x95,0x01,0x81,0x06,
  0xC0,0xC0
};

NimBLEServer* server;
NimBLECharacteristic* kbdIn;
NimBLECharacteristic* mouseIn;
Preferences prefs;

volatile bool ready = false;            // encrypted link to the one allowed PC
volatile uint16_t connHandle = BLE_HS_CONN_HANDLE_NONE;
bool paused = false;
uint8_t kbd[8] = {0};
uint8_t mouse[6] = {0};                 // buttons, xLo, xHi, yLo, yHi, wheel
uint32_t lastCmdMs = 0;
volatile bool dirtyKbd = false, dirtyMouse = false;  // report state not yet delivered to the PC

// Deliver pending reports. A failed notify (BLE buffers full during an RF fade) stays pending and loop()
// retries it, so a lost key-up can never leave a key auto-repeating on the PC.
bool flush() {
  if (!ready) {
    dirtyKbd = dirtyMouse = false;  // nothing to deliver to; the PC drops held input on disconnect
    return false;
  }
  if (dirtyKbd && kbdIn->notify(kbd, sizeof(kbd))) dirtyKbd = false;
  if (dirtyMouse && mouseIn->notify(mouse, sizeof(mouse))) {
    dirtyMouse = false;
    mouse[5] = 0;  // wheel is relative: never replay it in later reports
  }
  return !dirtyKbd && !dirtyMouse;
}

void releaseAll() {
  bool buttons = mouse[0] != 0;
  memset(kbd, 0, sizeof(kbd));
  mouse[0] = 0;
  mouse[5] = 0;
  dirtyKbd = true;
  // Only send a mouse report if a button was down: resending the agent's last position would yank the
  // owner's pointer back (absolute mouse) every time they press the kill switch.
  if (buttons) dirtyMouse = true;
  flush();
}

bool held() {
  if (mouse[0]) return true;
  for (uint8_t b : kbd) if (b) return true;
  return false;
}

class Callbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* s, NimBLEConnInfo& info) override {
    connHandle = info.getConnHandle();
    s->updateConnParams(info.getConnHandle(), 6, 12, 0, 400);  // 7.5-15 ms interval for snappy typing
  }
  void onDisconnect(NimBLEServer*, NimBLEConnInfo&, int) override {
    ready = false;
    connHandle = BLE_HS_CONN_HANDLE_NONE;
    memset(kbd, 0, sizeof(kbd));
    mouse[0] = 0;
    mouse[5] = 0;
  }
  void onAuthenticationComplete(NimBLEConnInfo& info) override {
    // One paired PC only. Input goes only to an encrypted, *bonded* peer while exactly one bond exists:
    // a device pairing without bonding would otherwise get an encrypted link without adding a bond.
    if (info.isEncrypted() && info.isBonded() && NimBLEDevice::getNumBonds() == 1) {
      ready = true;
      return;
    }
    if (info.isEncrypted()) NimBLEDevice::deleteBond(info.getIdAddress());  // a second PC, or no-bond pairing
    server->disconnect(info);  // failed encryption (e.g. link glitch): drop it but keep the real PC's bond
  }
};

bool gate() {
  if (paused) { Serial.println("ERR paused"); return false; }
  if (!ready) { Serial.println("ERR not connected"); return false; }
  return true;
}

void handle(const char* line) {
  lastCmdMs = millis();
  int a[7];
  if (!strcmp(line, "S")) {
    Serial.printf("OK connected=%d paused=%d bonded=%d\n", ready ? 1 : 0, paused ? 1 : 0, NimBLEDevice::getNumBonds() > 0 ? 1 : 0);
  } else if (!strcmp(line, "R")) {
    releaseAll();
    Serial.println("OK");
  } else if (!strcmp(line, "B")) {
    releaseAll();
    NimBLEDevice::deleteAllBonds();
    ready = false;
    if (connHandle != BLE_HS_CONN_HANDLE_NONE) server->disconnect(connHandle);
    Serial.println("OK");
  } else if (line[0] == 'K' && sscanf(line + 1, "%d %d %d %d %d %d %d", &a[0], &a[1], &a[2], &a[3], &a[4], &a[5], &a[6]) == 7) {
    for (int v : a) if (v < 0 || v > 255) { Serial.println("ERR bad command"); return; }
    if (!gate()) return;
    kbd[0] = a[0];
    kbd[1] = 0;
    for (int i = 0; i < 6; i++) kbd[2 + i] = a[1 + i];
    dirtyKbd = true;
    Serial.println(flush() ? "OK" : "ERR send failed");
  } else if (line[0] == 'M' && sscanf(line + 1, "%d %d %d %d", &a[0], &a[1], &a[2], &a[3]) == 4) {
    if (a[0] < 0 || a[0] > 7 || a[1] < 0 || a[1] > 32767 || a[2] < 0 || a[2] > 32767 || a[3] < -127 || a[3] > 127) {
      Serial.println("ERR bad command");
      return;
    }
    if (!gate()) return;
    if (a[1] == 0 && a[2] == 0) a[1] = 1;  // Windows ignores an absolute (0,0); (1,0) is the same pixel
    mouse[0] = a[0];
    mouse[1] = a[1] & 0xFF; mouse[2] = a[1] >> 8;
    mouse[3] = a[2] & 0xFF; mouse[4] = a[2] >> 8;
    mouse[5] = (uint8_t)(int8_t)a[3];
    dirtyMouse = true;
    Serial.println(flush() ? "OK" : "ERR send failed");
  } else {
    Serial.println("ERR bad command");
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(KILL_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  prefs.begin("hid", false);
  paused = prefs.getBool("paused", false);  // a reset must not silently un-pause

  NimBLEDevice::init("ABKey HID");
  NimBLEDevice::setSecurityAuth(true, false, true);
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
  server = NimBLEDevice::createServer();
  server->setCallbacks(new Callbacks());
  server->advertiseOnDisconnect(true);

  NimBLEHIDDevice* hid = new NimBLEHIDDevice(server);
  hid->setManufacturer("ABKey HID");
  hid->setPnp(0x02, 0x303A, 0x8001, 0x0100);
  hid->setHidInfo(0x00, 0x01);
  hid->setReportMap((uint8_t*)REPORT_MAP, sizeof(REPORT_MAP));
  kbdIn = hid->getInputReport(1);
  mouseIn = hid->getInputReport(2);
  hid->setBatteryLevel(100);
  server->start();

  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->setAppearance(0x03C1);  // keyboard
  adv->addServiceUUID(hid->getHidService()->getUUID());
  adv->setName("ABKey HID");
  adv->start();
}

void loop() {
  static char buf[96];
  static size_t len = 0;
  static bool overflow = false;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c != '\n') {
      if (len < sizeof(buf) - 1) buf[len++] = c; else overflow = true;
      continue;
    }
    buf[len] = 0;
    if (overflow) Serial.println("ERR bad command"); else if (len) handle(buf);
    len = 0;
    overflow = false;
  }

  // Kill switch: BOOT toggles pause (saved to flash) and releases everything.
  static int lastBtn = HIGH;
  static uint32_t btnMs = 0;
  int b = digitalRead(KILL_PIN);
  if (b != lastBtn && millis() - btnMs > 50) {
    btnMs = millis();
    lastBtn = b;
    if (b == LOW) {
      paused = !paused;
      prefs.putBool("paused", paused);
      releaseAll();
    }
  }

  if (dirtyKbd || dirtyMouse) flush();  // retry reports a congested link refused

  // Stuck-input guard: nothing may stay held through serial silence.
  if (held() && millis() - lastCmdMs > GUARD_MS) releaseAll();

  // LED: paused = 5 Hz blink, connected = solid, otherwise 1 Hz blink.
  uint32_t period = paused ? 200 : 1000;
  bool on = (ready && !paused) || (millis() % period) < period / 2;
  digitalWrite(LED_PIN, on ? LOW : HIGH);
  delay(1);
}
