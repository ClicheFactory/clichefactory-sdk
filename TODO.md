# TODO

## Tag presigned uploads at upload time

Files uploaded through a presigned URL (`_upload.presign_and_upload_*`, the
upload step of `extract` / `to_markdown` in service mode) are only marked as
temporary when the service processes them. A file uploaded but never
processed is kept forever.

Fix: the presign response should return the object tagging to apply, signed
into the URL, and the SDK's PUT should send it as the `x-amz-tagging` header.
This needs the service side first; until the service signs the header, the
SDK must keep working without it. Older SDK versions that don't send the
header must keep working too.
