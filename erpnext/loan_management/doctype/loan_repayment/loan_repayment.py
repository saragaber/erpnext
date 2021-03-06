# -*- coding: utf-8 -*-
# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe, erpnext
import json
from frappe import _
from frappe.utils import flt, getdate
from six import iteritems
from frappe.model.document import Document
from frappe.utils import date_diff, add_days, getdate, add_months, get_first_day, get_datetime
from erpnext.controllers.accounts_controller import AccountsController
from erpnext.accounts.general_ledger import make_gl_entries
from erpnext.loan_management.doctype.loan_security_shortfall.loan_security_shortfall import update_shortfall_status

class LoanRepayment(AccountsController):

	def validate(self):
		amounts = calculate_amounts(self.against_loan, self.posting_date, self.payment_type)
		self.set_missing_values(amounts)

	def before_submit(self):
		self.mark_as_paid()

	def on_submit(self):
		self.make_gl_entries()

	def on_cancel(self):
		self.mark_as_unpaid()
		self.make_gl_entries(cancel=1)

	def set_missing_values(self, amounts):
		if not self.posting_date:
			self.posting_date = get_datetime()

		if not self.cost_center:
			self.cost_center = erpnext.get_default_cost_center(self.company)

		if not self.interest_payable:
			self.interest_payable = amounts['interest_amount']

		if not self.penalty_amount:
			self.penalty_amount = amounts['penalty_amount']

		if not self.pending_principal_amount:
			self.pending_principal_amount = amounts['pending_principal_amount']

		if not self.payable_principal_amount and self.is_term_loan:
			self.payable_principal_amount = amounts['payable_principal_amount']

		if not self.payable_amount:
			self.payable_amount = amounts['payable_amount']

		if amounts.get('paid_accrual_entries'):
			self.paid_accrual_entries = frappe.as_json(amounts.get('paid_accrual_entries'))

		if amounts.get('due_date'):
			self.due_date = amounts.get('due_date')

	def mark_as_paid(self):
		paid_entries = []
		paid_amount = self.amount_paid
		interest_paid = paid_amount

		if not paid_amount:
			frappe.throw(_("Amount paid cannot be zero"))

		if self.amount_paid < self.penalty_amount:
			msg = _("Paid amount cannot be less than {0}").format(self.penalty_amount)
			frappe.throw(msg)

		if self.payment_type == "Loan Closure" and flt(self.amount_paid, 2) < flt(self.payable_amount, 2):
			msg = _("Amount of {0} is required for Loan closure").format(self.payable_amount)
			frappe.throw(msg)

		loan = frappe.get_doc("Loan", self.against_loan)

		if self.paid_accrual_entries:
			paid_accrual_entries = json.loads(self.paid_accrual_entries)

		if paid_amount - self.penalty_amount > 0 and self.paid_accrual_entries:

			interest_paid = paid_amount - self.penalty_amount

			for lia, interest_amount in iteritems(paid_accrual_entries):
				if interest_amount <= interest_paid:
					paid_entries.append(lia)
					interest_paid -= interest_amount
				elif interest_paid:
					self.partial_paid_entry = frappe.as_json({"name": lia, "interest_amount": interest_amount})
					frappe.db.set_value("Loan Interest Accrual", lia, "interest_amount",
						interest_amount - interest_paid)
					interest_paid = 0

		if paid_entries:
			self.paid_accrual_entries = frappe.as_json(paid_entries)
		else:
			self.paid_accrual_entries = ""

		if interest_paid:
			self.principal_amount_paid = interest_paid

		if paid_entries:
			frappe.db.sql("""UPDATE `tabLoan Interest Accrual`
				SET is_paid = 1 where name in (%s)""" #nosec
				% ", ".join(['%s']*len(paid_entries)), tuple(paid_entries))

		if flt(loan.total_principal_paid + self.principal_amount_paid, 2) >= flt(loan.total_payment, 2):
			frappe.db.set_value("Loan", self.against_loan, "status", "Loan Closure Requested")

		frappe.db.sql(""" UPDATE `tabLoan` SET total_amount_paid = %s, total_principal_paid = %s
			WHERE name = %s """, (loan.total_amount_paid + self.amount_paid,
			loan.total_principal_paid + self.principal_amount_paid, self.against_loan))

		update_shortfall_status(self.against_loan, self.principal_amount_paid)

	def mark_as_unpaid(self):

		loan = frappe.get_doc("Loan", self.against_loan)

		if self.paid_accrual_entries:
			paid_accrual_entries = json.loads(self.paid_accrual_entries)

		if self.paid_accrual_entries:
			frappe.db.sql("""UPDATE `tabLoan Interest Accrual`
				SET is_paid = 0 where name in (%s)""" #nosec
				% ", ".join(['%s']*len(paid_accrual_entries)), tuple(paid_accrual_entries))

		if self.partial_paid_entry:
			partial_paid_entry = json.loads(self.partial_paid_entry)
			frappe.db.set_value("Loan Interest Accrual", partial_paid_entry["name"], "interest_amount",
				partial_paid_entry["interest_amount"])

		frappe.db.sql(""" UPDATE `tabLoan` SET total_amount_paid = %s, total_principal_paid = %s
			WHERE name = %s """, (loan.total_amount_paid - self.amount_paid,
			loan.total_principal_paid - self.principal_amount_paid, self.against_loan))

		if loan.status == "Loan Closure Requested":
			frappe.db.set_value("Loan", self.against_loan, "status", "Disbursed")

	def make_gl_entries(self, cancel=0, adv_adj=0):
		gle_map = []
		loan_details = frappe.get_doc("Loan", self.against_loan)

		if self.penalty_amount:
			gle_map.append(
				self.get_gl_dict({
					"account": loan_details.loan_account,
					"against": loan_details.payment_account,
					"debit": self.penalty_amount,
					"debit_in_account_currency": self.penalty_amount,
					"against_voucher_type": "Loan",
					"against_voucher": self.against_loan,
					"remarks": _("Against Loan:") + self.against_loan,
					"cost_center": self.cost_center,
					"party_type": self.applicant_type,
					"party": self.applicant,
					"posting_date": getdate(self.posting_date)
				})
			)

			gle_map.append(
				self.get_gl_dict({
					"account": loan_details.penalty_income_account,
					"against": loan_details.payment_account,
					"credit": self.penalty_amount,
					"credit_in_account_currency": self.penalty_amount,
					"against_voucher_type": "Loan",
					"against_voucher": self.against_loan,
					"remarks": _("Against Loan:") + self.against_loan,
					"cost_center": self.cost_center,
					"party_type": self.applicant_type,
					"party": self.applicant,
					"posting_date": getdate(self.posting_date)
				})
			)

		gle_map.append(
			self.get_gl_dict({
				"account": loan_details.payment_account,
				"against": loan_details.loan_account + ", " + loan_details.interest_income_account
						+ ", " + loan_details.penalty_income_account,
				"debit": self.amount_paid,
				"debit_in_account_currency": self.amount_paid ,
				"against_voucher_type": "Loan",
				"against_voucher": self.against_loan,
				"remarks": _("Against Loan:") + self.against_loan,
				"cost_center": self.cost_center,
				"party_type": self.applicant_type,
				"party": self.applicant,
				"posting_date": getdate(self.posting_date)
			})
		)

		gle_map.append(
			self.get_gl_dict({
				"account": loan_details.loan_account,
				"party_type": loan_details.applicant_type,
				"party": loan_details.applicant,
				"against": loan_details.payment_account,
				"credit": self.amount_paid,
				"credit_in_account_currency": self.amount_paid,
				"against_voucher_type": "Loan",
				"against_voucher": self.against_loan,
				"remarks": _("Against Loan:") + self.against_loan,
				"cost_center": self.cost_center,
				"posting_date": getdate(self.posting_date)
			})
		)

		if gle_map:
			make_gl_entries(gle_map, cancel=cancel, adv_adj=adv_adj)

def create_repayment_entry(loan, applicant, company, posting_date, loan_type,
	payment_type, interest_payable, payable_principal_amount, amount_paid, penalty_amount=None):

	lr = frappe.get_doc({
		"doctype": "Loan Repayment",
		"against_loan": loan,
		"payment_type": payment_type,
		"company": company,
		"posting_date": posting_date,
		"applicant": applicant,
		"penalty_amount": penalty_amount,
		"interets_payable": interest_payable,
		"payable_principal_amount": payable_principal_amount,
		"amount_paid": amount_paid,
		"loan_type": loan_type
	}).insert()

	return lr

def get_accrued_interest_entries(against_loan):
	accrued_interest_entries = frappe.get_all("Loan Interest Accrual",
		fields=["name", "interest_amount", "posting_date", "payable_principal_amount"],
		filters = {
			"loan": against_loan,
			"is_paid": 0
		}, order_by="posting_date")

	return accrued_interest_entries

# This function returns the amounts that are payable at the time of loan repayment based on posting date
# So it pulls all the unpaid Loan Interest Accrual Entries and calculates the penalty if applicable

def get_amounts(amounts, against_loan, posting_date, payment_type):

	against_loan_doc = frappe.get_doc("Loan", against_loan)
	loan_type_details = frappe.get_doc("Loan Type", against_loan_doc.loan_type)
	accrued_interest_entries = get_accrued_interest_entries(against_loan_doc.name)

	pending_accrual_entries = {}

	total_pending_interest = 0
	penalty_amount = 0
	payable_principal_amount = 0
	final_due_date = ''

	for entry in accrued_interest_entries:
		# Loan repayment due date is one day after the loan interest is accrued
		# no of late days are calculated based on loan repayment posting date
		# and if no_of_late days are positive then penalty is levied

		due_date = add_days(entry.posting_date, 1)
		no_of_late_days = date_diff(posting_date,
					add_days(due_date, loan_type_details.grace_period_in_days)) + 1

		if no_of_late_days > 0 and (not against_loan_doc.repay_from_salary):
			penalty_amount += (entry.interest_amount * (loan_type_details.penalty_interest_rate / 100) * no_of_late_days)/365

		total_pending_interest += entry.interest_amount
		payable_principal_amount += entry.payable_principal_amount

		pending_accrual_entries.setdefault(entry.name, entry.interest_amount)
		final_due_date = due_date

	pending_principal_amount = against_loan_doc.total_payment - against_loan_doc.total_principal_paid - against_loan_doc.total_interest_payable

	if payment_type == "Loan Closure" and not payable_principal_amount:
		pending_days = date_diff(posting_date, entry.posting_date) + 1
		payable_principal_amount = pending_principal_amount
		per_day_interest = (payable_principal_amount * (loan_type_details.rate_of_interest / 100))/365
		total_pending_interest += (pending_days * per_day_interest)

	amounts["pending_principal_amount"] = pending_principal_amount
	amounts["payable_principal_amount"] = payable_principal_amount
	amounts["interest_amount"] = total_pending_interest
	amounts["penalty_amount"] = penalty_amount
	amounts["payable_amount"] = payable_principal_amount + total_pending_interest + penalty_amount
	amounts["paid_accrual_entries"] = pending_accrual_entries

	if final_due_date:
		amounts["due_date"] = final_due_date

	return amounts

@frappe.whitelist()
def calculate_amounts(against_loan, posting_date, payment_type):

	amounts = {
		'penalty_amount': 0.0,
		'interest_amount': 0.0,
		'pending_principal_amount': 0.0,
		'payable_principal_amount': 0.0,
		'payable_amount': 0.0,
		'due_date': ''
	}

	amounts = get_amounts(amounts, against_loan, posting_date, payment_type)

	return amounts



