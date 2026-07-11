import cv2
import numpy as np
import tensorflow as tf

# The underlying Keras FER model emits 7 classes. The hard contract requires
# all_scores to expose the full 10-label vocabulary the mobile/AWS path uses,
# so the 3 classes the model cannot predict are reported as 0.0.
MODEL_LABELS = ["angry", "disgust", "fear", "happy", "neutral", "sad", "surprise"]
CONTRACT_LABELS = [
    "angry", "disgust", "fear", "happy", "sad",
    "surprise", "neutral", "calm", "excited", "tired",
]
MODEL_VERSION = "1.0.0-fer7"

model = tf.keras.models.load_model("models/emotion_model.keras")


def predict_emotion(face_img: np.ndarray) -> tuple[str, float, dict[str, float]]:
    gray_face = cv2.cvtColor(face_img, cv2.COLOR_BGR2GRAY)
    resized = cv2.equalizeHist(cv2.resize(gray_face, (48, 48)))
    inp = np.reshape(resized.astype("float32") / 255.0, (1, 48, 48, 1))

    scores = model.predict(inp, verbose=0)[0]

    if not np.isclose(scores.sum(), 1.0, atol=1e-3) or (scores < 0).any():
        exp_scores = np.exp(scores - np.max(scores))
        scores = exp_scores / exp_scores.sum()

    idx = int(np.argmax(scores))
    label = MODEL_LABELS[idx]
    confidence = round(float(scores[idx]), 4)

    model_scores = {MODEL_LABELS[i]: float(scores[i]) for i in range(len(MODEL_LABELS))}
    all_scores = {
        name: round(model_scores.get(name, 0.0), 4) for name in CONTRACT_LABELS
    }
    return label, confidence, all_scores
