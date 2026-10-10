function encodeBase64Url(value: ArrayBuffer | null): string | null {
  if (value === null) {
    return null;
  }
  const bytes = new Uint8Array(value);
  let binary = '';
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  const encoded = window.btoa(binary);
  return encoded.replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
}

export const decodeCreationOptions = (
  options: PublicKeyCredentialCreationOptionsJSON,
): PublicKeyCredentialCreationOptions => PublicKeyCredential.parseCreationOptionsFromJSON(options);

export const decodeRequestOptions = (
  options: PublicKeyCredentialRequestOptionsJSON,
): PublicKeyCredentialRequestOptions => PublicKeyCredential.parseRequestOptionsFromJSON(options);

function serializeCredential(credential: PublicKeyCredential): Record<string, unknown> {
  const common = {
    id: credential.id,
    rawId: encodeBase64Url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    clientExtensionResults: credential.getClientExtensionResults(),
  };

  const source = credential.response;
  let binaryFields: Record<string, ArrayBuffer | null>;
  let responseDetails: Record<string, unknown> = {};

  if (source instanceof AuthenticatorAttestationResponse) {
    binaryFields = {
      attestationObject: source.attestationObject,
      clientDataJSON: source.clientDataJSON,
    };
    responseDetails = { transports: source.getTransports() };
  } else if (source instanceof AuthenticatorAssertionResponse) {
    binaryFields = {
      authenticatorData: source.authenticatorData,
      clientDataJSON: source.clientDataJSON,
      signature: source.signature,
      userHandle: source.userHandle,
    };
  } else {
    throw new Error('Unsupported WebAuthn credential response');
  }

  const response = Object.fromEntries(
    Object.entries(binaryFields).map(([name, value]) => [name, encodeBase64Url(value)]),
  );
  return { ...common, response: { ...response, ...responseDetails } };
}

export function isWebAuthnSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.PublicKeyCredential !== 'undefined' &&
    typeof window.PublicKeyCredential.parseCreationOptionsFromJSON === 'function' &&
    typeof window.PublicKeyCredential.parseRequestOptionsFromJSON === 'function' &&
    typeof window.navigator.credentials?.create === 'function' &&
    typeof window.navigator.credentials?.get === 'function'
  );
}

export async function createPasskey(options: PublicKeyCredentialCreationOptionsJSON): Promise<Record<string, unknown>> {
  const credential = await navigator.credentials.create({
    publicKey: decodeCreationOptions(options),
  });
  if (!(credential instanceof PublicKeyCredential)) {
    throw new Error('Passkey registration did not return a public-key credential');
  }
  return serializeCredential(credential);
}

export async function getPasskeyAssertion(
  options: PublicKeyCredentialRequestOptionsJSON,
): Promise<Record<string, unknown>> {
  const publicKey = decodeRequestOptions(options);
  const credential = await navigator.credentials.get({ publicKey });
  if (!(credential instanceof PublicKeyCredential)) {
    throw new Error('Passkey authorization did not return a public-key credential');
  }
  return serializeCredential(credential);
}
