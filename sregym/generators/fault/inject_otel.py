import json
import subprocess
import time

from sregym.generators.fault.base import FaultInjector
from sregym.service.kubectl import KubeCtl

# Maps feature flags to the deployment(s) that consume them.
# After restarting flagd, these services must also be restarted
# so their OpenFeature SDK reconnects and picks up the new flag value
# immediately instead of falling back to code-level defaults during
# the gRPC EventStream reconnection window.
# Source: https://opentelemetry.io/docs/demo/feature-flags/
_FLAG_TO_DEPLOYMENTS: dict[str, list[str]] = {
    "adFailure": ["ad"],
    "adHighCpu": ["ad"],
    "adManualGc": ["ad"],
    "cartFailure": ["cart"],
    "paymentFailure": ["payment"],
    "paymentUnreachable": ["checkout"],
    "productCatalogFailure": ["product-catalog"],
    "kafkaQueueProblems": ["kafka"],
    "imageSlowLoad": ["frontend"],
    "loadGeneratorFloodHomepage": ["load-generator"],
    "failedReadinessProbe": ["cart"],
    "recommendationCacheFailure": ["recommendation"],
    "emailMemoryLeak": ["email"],
    "llmInaccurateResponse": ["llm"],
    "llmRateLimitError": ["llm"],
}

# Flags whose "enabled" variant is NOT the standard "on".
# These flags use percentage/multiplier variants instead of boolean on/off.
# Source: https://github.com/open-telemetry/opentelemetry-demo/blob/main/src/flagd/demo.flagd.json
# How long kubelet may take to project a ConfigMap change into an already-mounted volume.
_CONFIGMAP_SYNC_SECONDS = 75

_FLAG_INJECT_VARIANT: dict[str, str] = {
    "imageSlowLoad": "10sec",
    "paymentFailure": "100%",
    "emailMemoryLeak": "1000x",
}


class OtelFaultInjector(FaultInjector):
    def __init__(self, namespace: str, configmap_name: str | None = None, restart_consumers: bool = True):
        self.namespace = namespace
        self.kubectl = KubeCtl()
        self._configmap_name = configmap_name
        # A metastable fault needs the consumer to KEEP its warm state across the flag change,
        # so restarting it has to be optional. flagd watches the mounted file either way.
        self.restart_consumers = restart_consumers

    @property
    def configmap_name(self) -> str:
        """Find the ConfigMap that actually holds the flags, rather than assuming its name.

        The Helm chart names it after the release (`flagd-cm0` here), not `flagd-config`, so a
        hardcoded name made every flag fault raise ValueError on this cluster.
        """
        if self._configmap_name is None:
            self._configmap_name = self._discover_configmap()
        return self._configmap_name

    def _discover_configmap(self) -> str:
        out = self.kubectl.exec_command(f"kubectl get configmap -n {self.namespace} -o json")
        if isinstance(out, tuple):
            out = out[0]
        for item in json.loads(out or "{}").get("items", []):
            if "demo.flagd.json" in (item.get("data") or {}):
                return item["metadata"]["name"]
        return "flagd-config"

    def _restart_flagd_and_consumers(self, feature_flag: str) -> None:
        """Restart flagd and the consuming service(s) so the flag change takes effect immediately."""
        if not self.restart_consumers:
            # flagd re-reads the mounted file on its own; the only cost is kubelet's sync
            # period before the change reaches the volume.
            print("[flagd] leaving flagd and consumers running; waiting for the mount to sync")
            time.sleep(_CONFIGMAP_SYNC_SECONDS)
            return

        self.kubectl.exec_command(f"kubectl rollout restart deployment flagd -n {self.namespace}")

        for deployment in _FLAG_TO_DEPLOYMENTS.get(feature_flag, []):
            self.kubectl.exec_command(f"kubectl rollout restart deployment {deployment} -n {self.namespace}")

        # Wait for flagd to be ready before proceeding
        self.kubectl.exec_command(f"kubectl rollout status deployment flagd -n {self.namespace} --timeout=60s")
        for deployment in _FLAG_TO_DEPLOYMENTS.get(feature_flag, []):
            self.kubectl.exec_command(
                f"kubectl rollout status deployment {deployment} -n {self.namespace} --timeout=120s"
            )

    def inject_fault(self, feature_flag: str, variant: str | None = None):
        command = f"kubectl get configmap {self.configmap_name} -n {self.namespace} -o json"
        try:
            output = self.kubectl.exec_command(command)
            configmap = json.loads(output)
        except subprocess.CalledProcessError as e:
            raise ValueError(f"ConfigMap '{self.configmap_name}' not found in namespace '{self.namespace}'.") from e
        except json.JSONDecodeError as e:
            raise ValueError(f"Error decoding JSON for ConfigMap '{self.configmap_name}'.") from e

        flagd_data = json.loads(configmap["data"]["demo.flagd.json"])

        if feature_flag in flagd_data["flags"]:
            variant = variant or _FLAG_INJECT_VARIANT.get(feature_flag, "on")
            flagd_data["flags"][feature_flag]["defaultVariant"] = variant
        else:
            raise ValueError(f"Feature flag '{feature_flag}' not found in ConfigMap '{self.configmap_name}'.")

        updated_data = {"demo.flagd.json": json.dumps(flagd_data, indent=2)}
        self.kubectl.create_or_update_configmap(self.configmap_name, self.namespace, updated_data)

        self._restart_flagd_and_consumers(feature_flag)

        print(f"Fault injected: Feature flag '{feature_flag}' set to '{variant}'.")

    def recover_fault(self, feature_flag: str):
        command = f"kubectl get configmap {self.configmap_name} -n {self.namespace} -o json"
        try:
            output = self.kubectl.exec_command(command)
            configmap = json.loads(output)
        except subprocess.CalledProcessError as e:
            raise ValueError(f"ConfigMap '{self.configmap_name}' not found in namespace '{self.namespace}'.") from e
        except json.JSONDecodeError as e:
            raise ValueError(f"Error decoding JSON for ConfigMap '{self.configmap_name}'.") from e

        flagd_data = json.loads(configmap["data"]["demo.flagd.json"])

        if feature_flag in flagd_data["flags"]:
            flagd_data["flags"][feature_flag]["defaultVariant"] = "off"
        else:
            raise ValueError(f"Feature flag '{feature_flag}' not found in ConfigMap '{self.configmap_name}'.")

        updated_data = {"demo.flagd.json": json.dumps(flagd_data, indent=2)}
        self.kubectl.create_or_update_configmap(self.configmap_name, self.namespace, updated_data)

        self._restart_flagd_and_consumers(feature_flag)

        print(f"Fault recovered: Feature flag '{feature_flag}' set to 'off'.")


# Example usage:
# if __name__ == "__main__":
#     namespace = "astronomy-shop"
#     feature_flag = "adServiceFailure"

#     injector = OtelFaultInjector(namespace)

#     injector.inject_fault(feature_flag)
#     injector.recover_fault(feature_flag)
