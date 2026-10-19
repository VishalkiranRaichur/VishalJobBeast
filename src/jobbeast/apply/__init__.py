"""Safe, auditable application execution subsystem.

The engine is intentionally not imported here: the store imports the small
state module, and eager package imports would create a store/engine cycle.
"""
