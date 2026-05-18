from typing import Literal

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, create_model

from agent.graph._utils import _invoke_structured, _recent
from agent.graph.state import AvailableMonitors
from agent.schemas.monitor import SUPPORTED_TOKENS
from agent.services.deposit_earn import launch_deposit_earn_monitor

MonitorParamType = Literal["token", "address", "api_key", "string", "bool"]


class MonitorParam(BaseModel):
    name: str
    description: str
    type: MonitorParamType
    required: bool = True
    choices: list[str] | None = None


class MonitorSchema(BaseModel):
    type: AvailableMonitors
    description: str
    params: list[MonitorParam]


MONITOR_CATALOGUE: dict[AvailableMonitors, MonitorSchema] = {
    "lending": MonitorSchema(
        type="lending",
        description="Monitors deposit assets for lending across protocols and recommends the best yield for your asset.",
        params=[
            MonitorParam(
                name="token_symbol",
                description="Token to monitor",
                type="token",
                choices=list(SUPPORTED_TOKENS.keys()),
            ),
            MonitorParam(
                name="token_mint",
                description="On-chain mint address of the token",
                type="address",
                choices=list(SUPPORTED_TOKENS.values()),
            ),
            MonitorParam(
                name="jup_api_key",
                description="Jupiter API key for executor actions",
                type="api_key",
                required=False,
            ),
            MonitorParam(
                name="withdraw_address",
                description="The address to which the funds should be withdrawn on stopping the monitor",
                type="address",
            ),
        ],
    ),
}


async def extract_params(
    messages: list[BaseMessage],
    params: list[MonitorParam],
) -> dict[str, str | None]:
    fields = {param.name: (str | None, None) for param in params}
    ExtractedModel = create_model("ExtractedParams", **fields)

    param_descriptions = "\n".join(
        f"- {param.name}: {param.description}"
        + (f" (choices: {', '.join(param.choices)})" if param.choices else "")
        for param in params
    )
    json_shape = "{" + ", ".join(f'"{p.name}": null' for p in params) + "}"
    extraction_prompt = (
        "Extract the requested params from the conversation. "
        "Replace null with the actual value if the user mentioned it; keep null if not.\n\n"
        f"Params:\n{param_descriptions}\n\n"
        f"Respond with a JSON object only, no other text. Example shape: {json_shape}"
    )

    extracted = await _invoke_structured(
        [{"role": "system", "content": extraction_prompt}] + messages,
        ExtractedModel,
    )
    return {param.name: getattr(extracted, param.name, None) for param in params}


def launch_monitor(monitor_type: AvailableMonitors, monitor_params: dict[str, str]):
    match monitor_type:
        case "lending":
            return launch_deposit_earn_monitor(
                token_mint=monitor_params["token_mint"],
                token_symbol=monitor_params["token_symbol"],
                jup_api_key=monitor_params.get("jup_api_key"),
            )
