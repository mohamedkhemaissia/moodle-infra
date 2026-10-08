"""Hachage Argon2id borne ; aucun mot de passe dans les sorties ou les journaux."""
from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import VerificationError, InvalidHashError

HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1,
                        hash_len=32, salt_len=16, type=Type.ID)


def validate_password(password):
    if not isinstance(password, str) or not 15 <= len(password) <= 128:
        raise ValueError("Choisir un mot de passe de 15 a 128 caracteres.")
    if "\x00" in password:
        raise ValueError("Le mot de passe contient un caractere interdit.")
    return password


def hash_password(password):
    return HASHER.hash(validate_password(password))


def verify_password(encoded, password):
    if (not isinstance(encoded, str) or len(encoded) > 512
            or not isinstance(password, str) or not 1 <= len(password) <= 128):
        return False
    try:
        parameters = extract_parameters(encoded)
        # Un hachage incompatible ne peut imposer un cout arbitraire au serveur.
        if (parameters.type != Type.ID or parameters.version != 19
                or parameters.memory_cost != 19456 or parameters.time_cost != 2
                or parameters.parallelism != 1 or parameters.hash_len != 32
                or parameters.salt_len != 16):
            return False
        return HASHER.verify(encoded, password)
    except (VerificationError, InvalidHashError, ValueError, TypeError):
        return False
