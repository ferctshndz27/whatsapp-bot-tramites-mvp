def mask_phone(phone: str) -> str:
    return f"***{phone[-4:]}" if len(phone) > 4 else "***"
