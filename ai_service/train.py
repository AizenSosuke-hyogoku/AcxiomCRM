"""Build-time model training entry point. Writes artifacts/lead_model.joblib."""
import main
main.train_model()
if main.MODEL is None:
    raise SystemExit("Lead model training failed; inspect the output above.")
print(f"Lead-scoring model trained and saved to {main.MODEL_PATH}")
