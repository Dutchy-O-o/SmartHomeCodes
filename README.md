# SmartHomeCodes

Source code for a cloud-connected IoT **smart-home system** built on a **Raspberry Pi 5**, with a serverless **AWS IoT** backend.

## Repository layout
- **`raspberry-pi/`** — on-device Python for sensors, actuators, RFID, LEDs, and AWS IoT connectivity
- **`emotion-api/`** — FastAPI service for on-device (edge) facial-emotion recognition

## Hardware
| Component | Interface |
|---|---|
| MPU6050 accelerometer/gyroscope | **I2C** (`mpu6050_driver.py`) |
| MFRC522 RFID reader | **SPI** (`find_rf_ID.py`) |
| DHT11 temperature / humidity | GPIO |
| SG90 servo | **PWM** (`servo_test.py`, `servo_calibrate.py`) |
| WS2812B LED strip (27 px) | NeoPixel (`led_show.py`) |
| Camera (Picamera2) | edge AI (emotion recognition) |

## Cloud / IoT
- **AWS IoT Core** over MQTT with mutual-TLS
- **Fleet Provisioning** for device onboarding (`provisioning.py`, `fleet_provisioning_main.py`)
- Serverless backend: **AWS Lambda, API Gateway, RDS (PostgreSQL), DynamoDB, Cognito, S3**

## Security note
Device certificates and private keys are **not** included in this repository (see `.gitignore`).
Provision your own credentials through AWS IoT and place them under a local `certs/` directory.
