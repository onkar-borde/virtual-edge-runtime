/*
 * ver_bridge - Virtual Edge Runtime GPIO bridge firmware
 *
 * Turns an ESP32 into the GPIO header your laptop doesn't have.
 * Speaks the line protocol described in ver/backends/esp32/protocol.py.
 *
 * Flash with the Arduino IDE:
 *   Board:  "ESP32 Dev Module"   (Tools > Board > esp32)
 *   Port:   whatever COM port appears when you plug the board in
 *   Upload speed: 921600 (drop to 115200 if uploads fail)
 *
 * IMPORTANT: keep this in lockstep with FakeTransport in transport.py.
 * They implement the same rules; the tests assume they agree.
 *
 * SAFETY: motor power comes from its own supply, never from USB 5V.
 * Share ground between the ESP32 and the motor driver, nothing else.
 */

#include <Arduino.h>
#include <Wire.h>

#define FW_NAME     "ver_bridge"
#define FW_PROTOCOL 2
#define BAUD        115200
#define MAX_PIN     39

// If the host goes quiet for this long, shut everything down. A crashed
// Python script must not leave a motor running into a wall. This is the
// single most important thing in this file.
#define WATCHDOG_MS 1000

// I2C. Registers and addresses travel as hex, matching every datasheet
// you'll have open next to this.
#define I2C_MAX_BYTES 64

// PWM: the ESP32 has 16 LEDC channels. We hand them out on demand.
#define PWM_RESOLUTION 10        // 0..1023, matches PWM_MAX in protocol.py
#define PWM_CHANNELS   16

// Espressif changed the LEDC API in Arduino-ESP32 v3.0: ledcSetup +
// ledcAttachPin became a single ledcAttach, and ledcWrite now takes a pin
// instead of a channel. Support both, so this compiles whether the user
// installed the current core or an older one.
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  #define VER_LEDC_V3 1
#else
  #define VER_LEDC_V3 0
#endif

enum PinRole : uint8_t {
  ROLE_NONE = 0, ROLE_INPUT, ROLE_PULLUP, ROLE_OUTPUT, ROLE_PWM, ROLE_ANALOG
};

PinRole  roles[MAX_PIN + 1];
int8_t   pwmChannel[MAX_PIN + 1];   // -1 = no channel assigned (v2 only)
uint32_t pwmFreq[MAX_PIN + 1];      // last frequency set on this pin
bool     channelUsed[PWM_CHANNELS];
uint32_t lastCommandMs = 0;
bool     watchdogTripped = false;

String   inputLine;
bool     i2cReady = false;

// ---------------------------------------------------------------- helpers

bool pinValid(int pin) {
  return pin >= 0 && pin <= MAX_PIN;
}

// GPIO 6-11 are wired to the SPI flash chip. Driving them crashes the board.
bool pinReserved(int pin) {
  return pin >= 6 && pin <= 11;
}

// GPIO 34-39 are input-only on the ESP32. No output, no pullup, no PWM.
bool pinInputOnly(int pin) {
  return pin >= 34 && pin <= 39;
}

#if !VER_LEDC_V3
int8_t claimChannel(int pin) {
  if (pwmChannel[pin] >= 0) return pwmChannel[pin];
  for (int8_t ch = 0; ch < PWM_CHANNELS; ch++) {
    if (!channelUsed[ch]) {
      channelUsed[ch] = true;
      pwmChannel[pin] = ch;
      return ch;
    }
  }
  return -1;
}
#endif

// --- PWM shim: the only place that knows which core we're on -------------

bool pwmAttach(int pin, uint32_t freq) {
#if VER_LEDC_V3
  // v3 allocates a channel internally; nothing for us to track.
  return ledcAttach(pin, freq, PWM_RESOLUTION);
#else
  int8_t ch = claimChannel(pin);
  if (ch < 0) return false;
  ledcSetup(ch, freq, PWM_RESOLUTION);
  ledcAttachPin(pin, ch);
  return true;
#endif
}

void pwmWrite(int pin, uint32_t duty) {
#if VER_LEDC_V3
  ledcWrite(pin, duty);
#else
  if (pwmChannel[pin] >= 0) ledcWrite(pwmChannel[pin], duty);
#endif
}

bool pwmSetFrequency(int pin, uint32_t freq) {
#if VER_LEDC_V3
  return ledcChangeFrequency(pin, freq, PWM_RESOLUTION);
#else
  int8_t ch = pwmChannel[pin];
  if (ch < 0) return false;
  ledcSetup(ch, freq, PWM_RESOLUTION);
  ledcAttachPin(pin, ch);
  return true;
#endif
}

// Everything off. Called by the STOP command and by the watchdog.
void stopAll() {
  for (int pin = 0; pin <= MAX_PIN; pin++) {
    if (roles[pin] == ROLE_PWM) {
      pwmWrite(pin, 0);
    } else if (roles[pin] == ROLE_OUTPUT) {
      digitalWrite(pin, LOW);
    }
  }
}

// -------------------------------------------------------------------- i2c

void handleI2cInit(int sda, int scl, int khz) {
  Wire.begin(sda, scl, (uint32_t)khz * 1000UL);
  i2cReady = true;
  Serial.println("OK");
}

void handleI2cScan() {
  if (!i2cReady) { Serial.println("ERR i2c not initialised"); return; }
  Serial.print("OK");
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf(" %02X", addr);
    }
  }
  Serial.println();
}

void handleI2cRead(int addr, int reg, int len) {
  if (!i2cReady) { Serial.println("ERR i2c not initialised"); return; }
  if (len < 1 || len > I2C_MAX_BYTES) {
    Serial.println("ERR i2c length out of range");
    return;
  }

  // Repeated START, not STOP-then-START: a stop between the register write
  // and the read lets another master interleave, and some chips reset their
  // internal pointer on a stop. endTransmission(false) keeps the bus held.
  Wire.beginTransmission((uint8_t)addr);
  Wire.write((uint8_t)reg);
  if (Wire.endTransmission(false) != 0) {
    Serial.printf("ERR i2c nack at %02X\n", addr);
    return;
  }

  uint8_t got = Wire.requestFrom((uint8_t)addr, (uint8_t)len, (uint8_t)true);
  if (got != len) {
    Serial.printf("ERR i2c short read at %02X (%d of %d)\n", addr, got, len);
    return;
  }

  Serial.print("OK ");
  while (Wire.available()) {
    Serial.printf("%02X", Wire.read());
  }
  Serial.println();
}

void handleI2cWrite(int addr, int reg, const String& hexData) {
  if (!i2cReady) { Serial.println("ERR i2c not initialised"); return; }
  if (hexData.length() % 2 != 0 || hexData.length() / 2 > I2C_MAX_BYTES) {
    Serial.println("ERR i2c bad payload");
    return;
  }

  Wire.beginTransmission((uint8_t)addr);
  Wire.write((uint8_t)reg);
  for (size_t i = 0; i < hexData.length(); i += 2) {
    Wire.write((uint8_t)strtol(hexData.substring(i, i + 2).c_str(), NULL, 16));
  }
  if (Wire.endTransmission() != 0) {
    Serial.printf("ERR i2c nack at %02X\n", addr);
    return;
  }
  Serial.println("OK");
}

// ---------------------------------------------------------------- commands

void handleMode(int pin, const String& mode) {
  if (pinReserved(pin)) {
    Serial.printf("ERR pin %d is reserved for flash\n", pin);
    return;
  }
  if ((mode == "output" || mode == "pwm") && pinInputOnly(pin)) {
    Serial.printf("ERR pin %d is input-only on the ESP32\n", pin);
    return;
  }

  if (mode == "input") {
    pinMode(pin, INPUT);           roles[pin] = ROLE_INPUT;
  } else if (mode == "pullup") {
    pinMode(pin, INPUT_PULLUP);    roles[pin] = ROLE_PULLUP;
  } else if (mode == "output") {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, LOW);        roles[pin] = ROLE_OUTPUT;
  } else if (mode == "analog") {
    roles[pin] = ROLE_ANALOG;
  } else if (mode == "pwm") {
    if (!pwmAttach(pin, 1000)) {
      Serial.println("ERR no PWM channels left");
      return;
    }
    pwmFreq[pin] = 1000;
    pwmWrite(pin, 0);
    roles[pin] = ROLE_PWM;
  } else {
    Serial.printf("ERR unknown mode %s\n", mode.c_str());
    return;
  }
  Serial.println("OK");
}

void handleWrite(int pin, int value) {
  if (roles[pin] != ROLE_OUTPUT) {
    Serial.printf("ERR pin %d not configured as output\n", pin);
    return;
  }
  digitalWrite(pin, value ? HIGH : LOW);
  Serial.println("OK");
}

void handleRead(int pin) {
  if (roles[pin] != ROLE_INPUT && roles[pin] != ROLE_PULLUP &&
      roles[pin] != ROLE_OUTPUT) {
    Serial.printf("ERR pin %d not configured for reading\n", pin);
    return;
  }
  Serial.printf("OK %d\n", digitalRead(pin) == HIGH ? 1 : 0);
}

void handlePwm(int pin, int duty, int freq) {
  if (roles[pin] != ROLE_PWM) {
    Serial.printf("ERR pin %d not configured as pwm\n", pin);
    return;
  }

  if (duty < 0) duty = 0;
  if (duty > 1023) duty = 1023;

  // Reconfiguring the timer on every write glitches the output, which on a
  // motor is an audible tick. Only touch it when the frequency actually
  // changed -- which, in a PID loop, is never.
  if ((uint32_t)freq != pwmFreq[pin]) {
    if (!pwmSetFrequency(pin, (uint32_t)freq)) {
      Serial.println("ERR could not set pwm frequency");
      return;
    }
    pwmFreq[pin] = (uint32_t)freq;
  }

  pwmWrite(pin, (uint32_t)duty);
  Serial.println("OK");
}

void handleAdc(int pin) {
  if (roles[pin] != ROLE_ANALOG) {
    Serial.printf("ERR pin %d not configured as analog\n", pin);
    return;
  }
  Serial.printf("OK %d\n", analogRead(pin));
}

// ---------------------------------------------------------------- dispatch

void dispatch(String line) {
  line.trim();
  if (line.length() == 0) return;

  lastCommandMs = millis();
  watchdogTripped = false;

  // Split on spaces into at most 4 tokens: CMD ARG1 ARG2 ARG3
  String tok[4];
  int n = 0;
  int start = 0;
  while (n < 4 && start < (int)line.length()) {
    int sp = line.indexOf(' ', start);
    if (sp < 0 || n == 3) { tok[n++] = line.substring(start); break; }
    tok[n++] = line.substring(start, sp);
    start = sp + 1;
  }

  String cmd = tok[0];

  if (cmd == "PING") { Serial.println("OK"); return; }

  if (cmd == "INFO") {
    Serial.printf("OK %s %d pins=%d\n", FW_NAME, FW_PROTOCOL, MAX_PIN + 1);
    return;
  }

  if (cmd == "STOP") { stopAll(); Serial.println("OK"); return; }

  if (cmd == "I2CSCAN") { handleI2cScan(); return; }

  if (cmd == "I2CINIT") {
    if (n < 4) { Serial.println("ERR bad arguments for I2CINIT"); return; }
    handleI2cInit(tok[1].toInt(), tok[2].toInt(), tok[3].toInt());
    return;
  }

  if (cmd == "I2CREAD") {
    if (n < 4) { Serial.println("ERR bad arguments for I2CREAD"); return; }
    handleI2cRead(strtol(tok[1].c_str(), NULL, 16),
                  strtol(tok[2].c_str(), NULL, 16),
                  tok[3].toInt());
    return;
  }

  if (cmd == "I2CWRITE") {
    if (n < 3) { Serial.println("ERR bad arguments for I2CWRITE"); return; }
    handleI2cWrite(strtol(tok[1].c_str(), NULL, 16),
                   strtol(tok[2].c_str(), NULL, 16),
                   n > 3 ? tok[3] : String(""));
    return;
  }

  // Everything past here needs a pin argument.
  if (n < 2) { Serial.printf("ERR bad arguments for %s\n", cmd.c_str()); return; }
  int pin = tok[1].toInt();
  if (!pinValid(pin)) { Serial.printf("ERR pin %d out of range\n", pin); return; }

  if (cmd == "MODE") {
    if (n < 3) { Serial.println("ERR bad arguments for MODE"); return; }
    handleMode(pin, tok[2]);
  } else if (cmd == "WRITE") {
    if (n < 3) { Serial.println("ERR bad arguments for WRITE"); return; }
    handleWrite(pin, tok[2].toInt());
  } else if (cmd == "READ") {
    handleRead(pin);
  } else if (cmd == "PWM") {
    if (n < 4) { Serial.println("ERR bad arguments for PWM"); return; }
    handlePwm(pin, tok[2].toInt(), tok[3].toInt());
  } else if (cmd == "ADC") {
    handleAdc(pin);
  } else {
    Serial.printf("ERR unknown command %s\n", cmd.c_str());
  }
}

// ---------------------------------------------------------------- lifecycle

void setup() {
  Serial.begin(BAUD);
  for (int i = 0; i <= MAX_PIN; i++) {
    roles[i] = ROLE_NONE;
    pwmChannel[i] = -1;
    pwmFreq[i] = 0;
  }
  for (int i = 0; i < PWM_CHANNELS; i++) channelUsed[i] = false;

  inputLine.reserve(64);
  lastCommandMs = millis();

  // Unprompted, so the host knows a reset happened and its pin config is gone.
  Serial.printf("RDY %s %d\n", FW_NAME, FW_PROTOCOL);
}

void loop() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      dispatch(inputLine);
      inputLine = "";
    } else if (c != '\r') {
      if (inputLine.length() < 120) inputLine += c;
    }
  }

  // Watchdog. Only trips once per silence, so we don't spam stopAll().
  if (!watchdogTripped && (millis() - lastCommandMs > WATCHDOG_MS)) {
    stopAll();
    watchdogTripped = true;
  }
}
