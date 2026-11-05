"""Write CloudFormation parameter files for the stage-2 API and jobs stacks.

Reads outputs of the already-created stacks (umsal-backend-network, -database, -auth)
with the AWS CLI (read-only) and fetches the Cognito JWKS document over HTTPS, so the
Lambda in the VPC never has to reach the internet. Nothing is created or changed.

Usage (operator machine, logged in with the project profile):
  python infra/aws/stage2_params.py \
      --api-key lambda/<sha>.zip --migrate-key lambda/<sha>-migrate.zip
  aws cloudformation deploy ... --parameter-overrides file://.local/stage2/api-params.json
(--parameter-overrides accepts a JSON list of "Key=Value" strings via file://; verify
with a change set before executing.)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".local" / "stage2"
PROFILE = ["--profile", "ai-x-final", "--region", "ap-southeast-2"]


def stack_outputs(name: str) -> dict[str, str]:
    raw = subprocess.run(
        [
            "aws",
            "cloudformation",
            "describe-stacks",
            *PROFILE,
            "--stack-name",
            name,
            "--query",
            "Stacks[0].Outputs",
            "--output",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {item["OutputKey"]: item["OutputValue"] for item in json.loads(raw)}


def fetch_jwks(issuer: str) -> str:
    if not issuer.startswith("https://cognito-idp."):
        raise SystemExit("Unexpected issuer; refusing to fetch keys from it.")
    with urllib.request.urlopen(f"{issuer}/.well-known/jwks.json", timeout=10) as response:  # noqa: S310
        document = json.loads(response.read())
    if not document.get("keys"):
        raise SystemExit("The JWKS document has no keys.")
    return json.dumps(document, separators=(",", ":"))  # whole document, compact


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", help="artifact bucket (default: bootstrap stack output)")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--migrate-key", required=True)
    args = parser.parse_args(argv)

    network = stack_outputs("umsal-backend-network")
    database = stack_outputs("umsal-backend-database")
    auth = stack_outputs("umsal-backend-auth")
    bucket = args.bucket or stack_outputs("umsal-backend-bootstrap")["ArtifactBucket"]
    jwks = fetch_jwks(auth["Issuer"])

    api = {
        "ArtifactBucket": bucket,
        "ArtifactKey": args.api_key,
        "SubnetIds": network["PrivateSubnetIds"],
        "LambdaSecurityGroupId": network["LambdaSecurityGroupId"],
        "DbHost": database["ClusterEndpoint"],
        "DbClusterResourceId": database["ClusterResourceId"],
        "CognitoUserPoolId": auth["UserPoolId"],
        "CognitoAppClientIds": auth["WebClientId"],
        "CognitoJwks": jwks,
    }
    jobs = {
        "ArtifactBucket": bucket,
        "ApiArtifactKey": args.api_key,
        "MigrateArtifactKey": args.migrate_key,
        "SubnetIds": network["PrivateSubnetIds"],
        "LambdaSecurityGroupId": network["LambdaSecurityGroupId"],
        "DbHost": database["ClusterEndpoint"],
        "DbClusterResourceId": database["ClusterResourceId"],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    for name, values in (("api", api), ("jobs", jobs)):
        path = OUT / f"{name}-params.json"
        path.write_text(json.dumps([f"{k}={v}" for k, v in values.items()], indent=2))
        print(f"Wrote {path.relative_to(ROOT).as_posix()} ({len(values)} parameters)")
    print(f"JWKS fixed with {len(json.loads(jwks)['keys'])} keys; redeploy after a rotation.")


if __name__ == "__main__":
    main()
