"""DICOM specialist built on the OpenAI Agents SDK.

MedDataOS calls it at POST /v1/chat/completions. It answers from the case through
the datactrl tools MedDataOS sends (MedDataOS runs those), reads allowlisted
radiology reference sites, and analyses volumes in a sandboxed Python shell.
"""
