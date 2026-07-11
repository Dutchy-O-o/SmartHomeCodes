"""AWS IoT device provisioning."""
import os
import sys
import time
import threading
from awscrt import mqtt
from awsiot import mqtt_connection_builder, iotidentity
import configparser
import os

def get_home_id(file_path):
    config = configparser.ConfigParser()
    
    # Dosyanın varlığını kontrol edelim
    if not os.path.exists(file_path):
        return "Hata: home.conf dosyası bulunamadı."

    try:
        config.read(file_path)
        # 'SETTINGS' bölümü altındaki 'homeid' değerini çekiyoruz
        # Eğer bölüm adı farklıysa (örn: [DEFAULT]) orayı güncellemelisin
        home_id = config.get('SETTINGS', 'homeid')
        return home_id
    except Exception as e:
        return f"Bir hata oluştu: {e}"


file_name = 'home.conf'


ENDPOINT = "a1cxjn3ytw2lp0-ats.iot.us-east-1.amazonaws.com"
TEMPLATE_NAME = "provisioning_template"
SERIAL_NUMBER = get_home_id(file_name)

CERT_DIR = "certs"
PERM_CERT = os.path.join(CERT_DIR, "permanent-cert.pem.crt")
PERM_KEY = os.path.join(CERT_DIR, "permanent-private.key")
CLAIM_CERT = os.path.join(CERT_DIR, "claim-cert.pem.crt")
CLAIM_KEY = os.path.join(CERT_DIR, "claim-private.key")
ROOT_CA = os.path.join(CERT_DIR, "AmazonRootCA1.pem")

registration_complete = threading.Event()


def run_provisioning():
    """Execute AWS IoT device provisioning process."""
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
        print("\n[SYSTEM] Restarting with new identity...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
