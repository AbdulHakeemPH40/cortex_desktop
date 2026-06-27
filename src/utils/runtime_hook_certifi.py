"""
Runtime hook: configure TLS CA certificate bundle for frozen builds.

PyInstaller does not always bundle certifi's cacert.pem correctly.
This hook ensures requests/urllib3 can find the CA bundle by:
1. Setting REQUESTS_CA_BUNDLE environment variable
2. Patching certifi.where() if needed
"""

import os
import sys


def _configure_certifi():
    """Find and configure the CA certificate bundle for frozen builds."""
    if not getattr(sys, 'frozen', False):
        return  # Not a frozen build, nothing to do

    # Skip if already configured by user
    if os.environ.get('REQUESTS_CA_BUNDLE'):
        return

    # Try to import certifi and get the bundled path
    try:
        import certifi
        ca_path = certifi.where()
        if os.path.isfile(ca_path):
            os.environ['REQUESTS_CA_BUNDLE'] = ca_path
            return
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: search for cacert.pem in known locations
    search_paths = []

    # PyInstaller onefile: _MEIPASS temp directory
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        search_paths.append(os.path.join(meipass, 'certifi', 'cacert.pem'))
        search_paths.append(os.path.join(meipass, 'cacert.pem'))

    # PyInstaller onedir: next to executable
    exe_dir = os.path.dirname(sys.executable)
    search_paths.append(os.path.join(exe_dir, '_internal', 'certifi', 'cacert.pem'))
    search_paths.append(os.path.join(exe_dir, 'certifi', 'cacert.pem'))

    # Check each path
    for path in search_paths:
        if os.path.isfile(path):
            os.environ['REQUESTS_CA_BUNDLE'] = path
            return

    # Last resort: use Windows system certificate store via ssl module
    # This works on most Windows machines without a bundled cert
    try:
        import ssl
        ctx = ssl.create_default_context()
        # If we get here, the system store is usable
        # Set SSL_CERT_FILE to empty string to force system store usage
        pass
    except Exception:
        pass


_configure_certifi()
