#include <WiFi.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <MFRC522.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#define SS_PIN 5
#define RST_PIN 22

#define IR_SENSOR 27

#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64

#define OLED_ADDR 0x3C

MFRC522 rfid(SS_PIN, RST_PIN);
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

TaskHandle_t RFIDTaskHandle;
TaskHandle_t FaceTaskHandle;

const char* ssid = "YOUR_WIFI";
const char* password = "YOUR_PASSWORD";

String SERVER_URL = "http://your-server/api";

bool facultyVerified = false;
String facultyRFID = "";
String studentRFID = "";

String studentName = "";
String verifyStatus = "";

void displayMessage(String line1, String line2)
{
  display.clearDisplay();
  display.setCursor(0,0);
  display.setTextSize(1);
  display.println(line1);
  display.println(line2);
  display.display();
}

bool verifyFaculty(String rfidTag)
{
  HTTPClient http;

  String url = SERVER_URL + "/verify_faculty";

  http.begin(url);
  http.addHeader("Content-Type","application/json");

  String payload = "{\"rfid\":\""+rfidTag+"\"}";

  int code = http.POST(payload);

  if(code == 200)
  {
    String response = http.getString();
    http.end();
    return true;
  }

  http.end();
  return false;
}

void sendStudentRFID(String rfidTag)
{
  HTTPClient http;

  String url = SERVER_URL + "/rfid_scan";

  http.begin(url);
  http.addHeader("Content-Type","application/json");

  String payload = "{\"rfid\":\""+rfidTag+"\"}";

  int code = http.POST(payload);

  if(code == 200)
  {
    String response = http.getString();
    studentName = "Student";
    verifyStatus = "RFID Verified";
  }

  http.end();
}

void requestFaceCapture()
{
  HTTPClient http;

  String url = SERVER_URL + "/capture";

  http.begin(url);

  int code = http.GET();

  if(code == 200)
  {
    verifyStatus = "Face Verified";
  }

  http.end();
}

void RFIDTask(void * parameter)
{
  while(true)
  {
    if(!rfid.PICC_IsNewCardPresent())
    {
      vTaskDelay(100 / portTICK_PERIOD_MS);
      continue;
    }

    if(!rfid.PICC_ReadCardSerial())
    {
      vTaskDelay(100 / portTICK_PERIOD_MS);
      continue;
    }

    String rfidTag = "";

    for(byte i = 0; i < rfid.uid.size; i++)
    {
      rfidTag += String(rfid.uid.uidByte[i], HEX);
    }

    Serial.print("RFID: ");
    Serial.println(rfidTag);

    if(!facultyVerified)
    {
      displayMessage("Checking Faculty", "");

      bool status = verifyFaculty(rfidTag);

      if(status)
      {
        facultyVerified = true;
        facultyRFID = rfidTag;

        displayMessage("Faculty Verified","System Ready");
      }
      else
      {
        displayMessage("Invalid Faculty","Try Again");
      }
    }
    else
    {
      studentRFID = rfidTag;

      displayMessage("Student RFID","Processing");

      sendStudentRFID(studentRFID);

      displayMessage(studentName, verifyStatus);
    }

    rfid.PICC_HaltA();

    vTaskDelay(2000 / portTICK_PERIOD_MS);
  }
}

void FaceTask(void * parameter)
{
  while(true)
  {
    if(!facultyVerified)
    {
      vTaskDelay(1000 / portTICK_PERIOD_MS);
      continue;
    }

    int presence = digitalRead(IR_SENSOR);

    if(presence == HIGH)
    {
      Serial.println("Person detected");

      displayMessage("Capturing Face","Please Wait");

      requestFaceCapture();

      displayMessage(studentName, verifyStatus);
    }

    vTaskDelay(200 / portTICK_PERIOD_MS);
  }
}

void setup()
{
  Serial.begin(115200);

  pinMode(IR_SENSOR, INPUT);

  SPI.begin();
  rfid.PCD_Init();

  Wire.begin();

  if(!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR))
  {
    Serial.println("OLED failed");
    while(true);
  }

  display.clearDisplay();
  display.display();

  displayMessage("Verify Faculty","Scan RFID");

  WiFi.begin(ssid,password);

  while(WiFi.status()!=WL_CONNECTED)
  {
    delay(500);
    Serial.print(".");
  }

  Serial.println("WiFi Connected");

  xTaskCreatePinnedToCore(
    RFIDTask,
    "RFIDTask",
    5000,
    NULL,
    1,
    &RFIDTaskHandle,
    0
  );

  xTaskCreatePinnedToCore(
    FaceTask,
    "FaceTask",
    5000,
    NULL,
    1,
    &FaceTaskHandle,
    1
  );
}

void loop()
{
}