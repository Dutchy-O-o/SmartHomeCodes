import os
import sys
import time
import threading
from awscrt import mqtt
from awsiot import mqtt_connection_builder, iotidentity
import board
import adafruit_dht
from gpiozero import DigitalInputDevice
import random

ENDPOINT = "a1cxjn3ytw2lp0-ats.iot.us-east-1.amazonaws.com"
TEMPLATE_NAME = "provisioning_template" 
SERIAL_NUMBER = "alert_system"

CERT_DIR = "certs"
PERM_CERT = os.path.join(CERT_DIR, "permanent-cert.pem.crt")
PERM_KEY = os.path.join(CERT_DIR, "permanent-private.key")
CLAIM_CERT = os.path.join(CERT_DIR, "claim-cert.pem.crt")
CLAIM_KEY = os.path.join(CERT_DIR, "claim-private.key")
ROOT_CA = os.path.join(CERT_DIR, "AmazonRootCA1.pem")

registration_complete = threading.Event()

def restart_script():
    print("\n[SYSTEM] Restarting with new identity...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

def run_provisioning():
    print("\n[STEP 1] No permanent certificate. Starting registration process...")
    
    mqtt_connection = mqtt_connection_builder.mtls_from_path(
        endpoint=ENDPOINT,
        cert_filepath=CLAIM_CERT,
        pri_key_filepath=CLAIM_KEY,
        ca_filepath=ROOT_CA,
        client_id="ProvisioningClient_" + str(int(time.time())),
        clean_session=True,
        keep_alive_secs=30)

    mqtt_connection.connect().result()
    identity_client = iotidentity.IotIdentityClient(mqtt_connection)

    def on_register_accepted(response):
        print(f"[SUCCESS] AWS created '{response.thing_name}' thing!")
        registration_complete.set()

    def on_register_rejected(error):
        print(f"[ERROR] AWS rejected registration: {error.error_message}")
        registration_complete.set()

    def on_keys_accepted(response):
        print("[INFO] New certificates received, saving...")
        with open(PERM_CERT, "w") as f: f.write(response.certificate_pem)
        with open(PERM_KEY, "w") as f: f.write(response.private_key)

        identity_client.subscribe_to_register_thing_accepted(
            request=iotidentity.RegisterThingRequest(template_name=TEMPLATE_NAME),
            qos=mqtt.QoS.AT_LEAST_ONCE,
            callback=on_register_accepted
        )
        identity_client.subscribe_to_register_thing_rejected(
            request=iotidentity.RegisterThingRequest(template_name=TEMPLATE_NAME),
            qos=mqtt.QoS.AT_LEAST_ONCE,
            callback=on_register_rejected
        )

        print("[REQUEST] Sending RegisterThing...")
        register_request = iotidentity.RegisterThingRequest(
            template_name=TEMPLATE_NAME,
            certificate_ownership_token=response.certificate_ownership_token,
            parameters={"SerialNumber": SERIAL_NUMBER}
        )
        identity_client.publish_register_thing(register_request, mqtt.QoS.AT_LEAST_ONCE)


    identity_client.subscribe_to_create_keys_and_certificate_accepted(
        request=iotidentity.CreateKeysAndCertificateRequest(),
        qos=mqtt.QoS.AT_LEAST_ONCE,
        callback=on_keys_accepted
    )

    print("[REQUEST] Requesting new certificate set...")
    identity_client.publish_create_keys_and_certificate(
        request=iotidentity.CreateKeysAndCertificateRequest(),
        qos=mqtt.QoS.AT_LEAST_ONCE
    )

    print("[WAITING] Waiting for AWS confirmation (Max 30s)...")
    if not registration_complete.wait(timeout=30):
        print("[ERROR] Timeout! AWS did not respond.")
    else:
        time.sleep(2)
        restart_script()

def run_normal_operation():
    gas_sensor = DigitalInputDevice(15, pull_up=True)
    dht_device = adafruit_dht.DHT11(board.D14)

    print(f"\n[STEP 2] Normal mode: connecting as {SERIAL_NUMBER}...")
    
    mqtt_connection = mqtt_connection_builder.mtls_from_path(
        endpoint=ENDPOINT,
        cert_filepath=PERM_CERT,
        pri_key_filepath=PERM_KEY,
        ca_filepath=ROOT_CA,
        client_id=SERIAL_NUMBER,
        clean_session=False,
        keep_alive_secs=30)

    mqtt_connection.connect().result()
    print("[SUCCESS] AWS IoT connection established. Listening...")

    
    while True:
        try:
            temperature_c = dht_device.temperature
            humidity = dht_device.humidity
            
            if gas_sensor.value == 1:
                print("Gas leak detected!")
                mqtt_connection.publish(topic=f"{SERIAL_NUMBER}/gas", payload="gas leak detected", qos=mqtt.QoS.AT_LEAST_ONCE)
            else:
                print("No gas leak.")

            mqtt_connection.publish(topic=f"{SERIAL_NUMBER}/temperature", payload=str(temperature_c), qos=mqtt.QoS.AT_LEAST_ONCE)
            mqtt_connection.publish(topic=f"{SERIAL_NUMBER}/humidity", payload=str(humidity), qos=mqtt.QoS.AT_LEAST_ONCE)
        except RuntimeError as error:
            print(f"Reading error: {error.args[0]}")
            time.sleep(2.0)
            continue
        except Exception as error:
            dht_device.exit()
            raise error

        time.sleep(1.0)
    

if __name__ == "__main__":
    if not os.path.exists(PERM_CERT):
        run_provisioning()
    else:
        run_normal_operation()






