#!/usr/bin/env bash
ENDPOINT=$1
PRODUCT_ID=1
QUANTITY=10

if [ -z "$ENDPOINT" ]; then
  echo "Usage: $0 <pessimistic|optimistic>"
  exit 1
fi

URL="http://localhost:8080/api/orders/$ENDPOINT"

echo "Resetting product stock..."
curl -s -X POST http://localhost:8080/api/products/reset

echo "Starting concurrent test on $URL..."

for i in {1..20}
do
  curl -s -X POST -H "Content-Type: application/json" -d "{\"productId\": $PRODUCT_ID, \"quantity\": $QUANTITY, \"userId\": \"user$i\"}" $URL &
  sleep 0.05
done

wait
echo "Test finished. Check stats endpoint for results."
curl -s http://localhost:8080/api/orders/stats | jq || true
