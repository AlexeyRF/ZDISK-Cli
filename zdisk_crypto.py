import os
import secrets
import string
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.backends import default_backend
import shutil
class ZDiskCrypto:
    """Handles AES-256-GCM encryption/decryption with PBKDF2 key derivation."""
    
    ITERATIONS = 100_000
    SALT_SIZE = 16
    NONCE_SIZE = 12
    
    @staticmethod
    def generate_password(length=16):
        """Generates a reliable random password."""
        alphabet = string.ascii_letters + string.digits + string.punctuation
        return ''.join(secrets.choice(alphabet) for _ in range(length))

    def _derive_key(self, password: str, salt: bytes) -> bytes:
        """Derives a 256-bit key from a password and salt."""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=self.ITERATIONS,
            backend=default_backend()
        )
        return kdf.derive(password.encode())

    CHUNK_SIZE = 1024 * 1024 # 1MB chunks

    def encrypt_file(self, input_path: str, output_path: str, password: str):
        """Encrypts a file using chunked AES-256-GCM for memory efficiency."""
        salt = os.urandom(self.SALT_SIZE)
        key = self._derive_key(password, salt)
        nonce_base = os.urandom(self.NONCE_SIZE - 4) # 8 bytes base
        
        aesgcm = AESGCM(key)
        
        with open(input_path, 'rb') as f_in, open(output_path, 'wb') as f_out:
            f_out.write(salt)
            f_out.write(nonce_base)
            
            chunk_index = 0
            while True:
                data = f_in.read(self.CHUNK_SIZE)
                if not data:
                    break
                
                # Create nonce for this chunk: [base (8)] [index (4)]
                nonce = nonce_base + chunk_index.to_bytes(4, 'big')
                ciphertext = aesgcm.encrypt(nonce, data, None)
                
                # Each chunk in output: [len(4)] [ciphertext+tag]
                f_out.write(len(ciphertext).to_bytes(4, 'big'))
                f_out.write(ciphertext)
                chunk_index += 1

    def decrypt_file(self, input_path: str, output_path: str, password: str):
        """Decrypts a file using chunked AES-256-GCM."""
        try:
            with open(input_path, 'rb') as f_in:
                salt = f_in.read(self.SALT_SIZE)
                nonce_base = f_in.read(self.NONCE_SIZE - 4)
                
                key = self._derive_key(password, salt)
                aesgcm = AESGCM(key)
                
                with open(output_path, 'wb') as f_out:
                    chunk_index = 0
                    while True:
                        len_bytes = f_in.read(4)
                        if not len_bytes:
                            break
                        
                        chunk_len = int.from_bytes(len_bytes, 'big')
                        ciphertext = f_in.read(chunk_len)
                        if len(ciphertext) != chunk_len:
                             raise ValueError("Incomplete chunk")
                        
                        nonce = nonce_base + chunk_index.to_bytes(4, 'big')
                        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
                        f_out.write(plaintext)
                        chunk_index += 1
            return True
        except Exception as e:
            import logging
            logging.getLogger("zdisk_crypto").error(f"Decryption error: {e}")
            return False

if __name__ == "__main__":
    # Quick test
    crypto = ZDiskCrypto()
    pw = "test_password"
    with open("test.txt", "w") as f: f.write("Hello ZDisk!")
    crypto.encrypt_file("test.txt", "test.txt.enc", pw)
    success = crypto.decrypt_file("test.txt.enc", "test.txt.dec", pw)
    print(f"Decryption success: {success}")
    with open("test.txt.dec", "r") as f: print(f"Content: {f.read()}")
    os.remove("test.txt")
    os.remove("test.txt.enc")
    os.remove("test.txt.dec")
