/* Optional function for the buyer team to add to their existing page.
 * It exports the page's existing transactionLedger; it does not create sales.
 * Connect it to a button only after confirming the ledger is the official one.
 */
function exportSellerLedgerCSV() {
  const columns = [
    ["Event_ID", tx => tx._id],
    ["ISO_Timestamp", tx => tx.isoTimestamp],
    ["Tx_ID", tx => tx.txId],
    ["Selected_Seller", tx => tx.sellerName],
    ["Offer_ID", tx => tx.offerId],
    ["Units_Bought", tx => tx.unitsBought],
    ["Price_Paid", tx => tx.pricePaid],
    ["Remaining_Stock", tx => tx.remainingStock],
    ["Status", tx => tx.status],
  ];
  const cell = value => `"${String(value ?? "").replace(/"/g, '""')}"`;
  const lines = [columns.map(([name]) => cell(name)).join(",")];
  for (const tx of transactionLedger) {
    lines.push(columns.map(([, getValue]) => cell(getValue(tx))).join(","));
  }
  const url = URL.createObjectURL(new Blob([lines.join("\r\n")], {type: "text/csv;charset=utf-8"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = "buyer_results.csv";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
