# Code signing

Unsigned downloads trigger Windows SmartScreen's *"Windows protected your PC"*
warning, which stops many people installing. The release workflow can sign
both `Statusify.exe` and `Statusify-Setup-<version>.exe`; it just needs
credentials. Until they exist, releases build unsigned exactly as before.

## Option A: Azure Trusted Signing (wired up, ~US$10/month)

1. In the Azure portal, create a **Trusted Signing account** and complete
   **identity validation** (individual developers are supported; validation
   can take a few days).
2. Create a **certificate profile** (type *Public Trust*).
3. Create an **app registration** (Microsoft Entra ID), give it a client
   secret, and grant it the **Trusted Signing Certificate Profile Signer**
   role on the signing account.
4. Add these **repository secrets** (GitHub → Settings → Secrets and variables
   → Actions):

   | Secret | Value |
   | --- | --- |
   | `AZURE_TENANT_ID` | Directory (tenant) ID of the app registration |
   | `AZURE_CLIENT_ID` | Application (client) ID |
   | `AZURE_CLIENT_SECRET` | The client secret |
   | `AZURE_SIGNING_ENDPOINT` | Your account's region endpoint, e.g. `https://weu.codesigning.azure.net/` |
   | `AZURE_SIGNING_ACCOUNT` | Trusted Signing account name |
   | `AZURE_CERT_PROFILE` | Certificate profile name |

The next tagged release signs automatically: `AZURE_CLIENT_ID` being set is
what switches it on. Checksums are computed after signing, so the in-app
updater's hash check keeps working.

## Option B: SignPath Foundation (free for open source)

[SignPath](https://signpath.org/) signs open-source projects at no cost after
an application review. It uses its own GitHub integration rather than the
Azure step above; if accepted, replace the two `Sign …` steps in
`.github/workflows/release.yml` with SignPath's action.

## What signing does and doesn't fix

A signature proves the file came from you and wasn't altered. SmartScreen
additionally weighs *reputation*: a newly signed app can still show a warning
for its first downloads, which fades as the certificate accumulates clean
installs. Signing is the prerequisite for that reputation to build at all.
