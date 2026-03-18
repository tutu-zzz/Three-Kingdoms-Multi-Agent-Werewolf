import os


def load_local_env(env_path: str = ".env") -> None:
    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            if key and key not in os.environ:
                os.environ[key] = value


def mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def main() -> None:
    print(f".env exists: {os.path.exists('.env')}")
    print(f"before load: {'DASHSCOPE_API_KEY' in os.environ}")

    load_local_env()

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    print(f"after load: {api_key is not None}")

    if api_key:
        print(f"key preview: {mask_secret(api_key)}")
        print(f"key length: {len(api_key)}")
    else:
        print("DASHSCOPE_API_KEY not found")


if __name__ == "__main__":
    main()
