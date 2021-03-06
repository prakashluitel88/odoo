# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from openerp.osv import fields, osv
from openerp.tools import float_compare, float_round
from openerp.tools.translate import _
from openerp import SUPERUSER_ID, api, models
from openerp.exceptions import UserError
import logging
_logger = logging.getLogger(__name__)


class stock_inventory(osv.osv):
    _inherit = "stock.inventory"
    _columns = {
        'accounting_date': fields.date('Force Accounting Date', help="Choose the accounting date at which you want to value the stock moves created by the inventory instead of the default one (the inventory end date)"),
    }

    def post_inventory(self, cr, uid, ids, context=None):
        if context is None:
            context = {}
        ctx = context.copy()
        inv = self.browse(cr, uid, ids, context=context)[0]
        if inv.accounting_date:
            ctx['force_period_date'] = inv.accounting_date
        return super(stock_inventory, self).post_inventory(cr, uid, ids, context=ctx)


class account_invoice_line(osv.osv):
    _inherit = "account.invoice.line"

    def _get_anglo_saxon_price_unit(self):
        self.ensure_one()
        return self.product_id.standard_price

    def _get_price(self, cr, uid, inv, company_currency, i_line, price_unit):
        cur_obj = self.pool.get('res.currency')
        if inv.currency_id.id != company_currency:
            price = cur_obj.compute(cr, uid, company_currency, inv.currency_id.id, price_unit * i_line.quantity, context={'date': inv.date_invoice})
        else:
            price = price_unit * i_line.quantity
        return round(price, inv.currency_id.decimal_places)

    def get_invoice_line_account(self, type, product, fpos, company):
        if company.anglo_saxon_accounting and type in ('in_invoice', 'in_refund') and product and product.type == 'product':
            accounts = product.product_tmpl_id.get_product_accounts(fiscal_pos=fpos)
            return accounts['stock_input']
        return super(account_invoice_line, self).get_invoice_line_account(type, product, fpos, company)

class account_invoice(osv.osv):
    _inherit = "account.invoice"

    @api.model
    def invoice_line_move_line_get(self):
        res = super(account_invoice,self).invoice_line_move_line_get()
        if self.company_id.anglo_saxon_accounting:
            if self.type in ('out_invoice','out_refund'):
                for i_line in self.invoice_line_ids:
                    res.extend(self._anglo_saxon_sale_move_lines(i_line))
        return res

    @api.model
    def _anglo_saxon_sale_move_lines(self, i_line):
        """Return the additional move lines for sales invoices and refunds.

        i_line: An account.invoice.line object.
        res: The move line entries produced so far by the parent move_line_get.
        """
        inv = i_line.invoice_id
        company_currency = inv.company_id.currency_id.id

        if i_line.product_id.type  == 'product' and i_line.product_id.valuation == 'real_time':
            fpos = i_line.invoice_id.fiscal_position_id
            accounts = i_line.product_id.product_tmpl_id.get_product_accounts(fiscal_pos=fpos)
            # debit account dacc will be the output account
            dacc = accounts['stock_output'].id
            # credit account cacc will be the expense account
            cacc = accounts['expense'].id
            if dacc and cacc:
                price_unit = i_line._get_anglo_saxon_price_unit()
                return [
                    {
                        'type':'src',
                        'name': i_line.name[:64],
                        'price_unit': price_unit,
                        'quantity': i_line.quantity,
                        'price': self.env['account.invoice.line']._get_price(inv, company_currency, i_line, price_unit),
                        'account_id':dacc,
                        'product_id':i_line.product_id.id,
                        'uom_id':i_line.uom_id.id,
                        'account_analytic_id': False,
                    },

                    {
                        'type':'src',
                        'name': i_line.name[:64],
                        'price_unit': price_unit,
                        'quantity': i_line.quantity,
                        'price': -1 * self.env['account.invoice.line']._get_price(inv, company_currency, i_line, price_unit),
                        'account_id':cacc,
                        'product_id':i_line.product_id.id,
                        'uom_id':i_line.uom_id.id,
                        'account_analytic_id': False,
                    },
                ]
        return []


#----------------------------------------------------------
# Stock Location
#----------------------------------------------------------

class stock_location(osv.osv):
    _inherit = "stock.location"

    _columns = {
        'valuation_in_account_id': fields.many2one('account.account', 'Stock Valuation Account (Incoming)', domain=[('internal_type', '=', 'other'), ('deprecated', '=', False)],
                                                   help="Used for real-time inventory valuation. When set on a virtual location (non internal type), "
                                                        "this account will be used to hold the value of products being moved from an internal location "
                                                        "into this location, instead of the generic Stock Output Account set on the product. "
                                                        "This has no effect for internal locations."),
        'valuation_out_account_id': fields.many2one('account.account', 'Stock Valuation Account (Outgoing)', domain=[('internal_type', '=', 'other'), ('deprecated', '=', False)],
                                                   help="Used for real-time inventory valuation. When set on a virtual location (non internal type), "
                                                        "this account will be used to hold the value of products being moved out of this location "
                                                        "and into an internal location, instead of the generic Stock Output Account set on the product. "
                                                        "This has no effect for internal locations."),
    }

#----------------------------------------------------------
# Quants
#----------------------------------------------------------

class stock_quant(osv.osv):
    _inherit = "stock.quant"

    @api.multi
    def _compute_inventory_value(self):
        real_value_quants = self.filtered(lambda quant: quant.product_id.cost_method == 'real')
        for quant in real_value_quants:
            quant.inventory_value = quant.cost * quant.qty
        other = self - real_value_quants
        return super(stock_quant, other)._compute_inventory_value()

    @api.cr_uid_ids_context
    def _price_update(self, cr, uid, quant_ids, newprice, context=None):
        ''' This function is called at the end of negative quant reconciliation and does the accounting entries adjustemnts and the update of the product cost price if needed
        '''
        if context is None:
            context = {}
        account_move_obj = self.pool['account.move']
        super(stock_quant, self)._price_update(cr, uid, quant_ids, newprice, context=context)
        for quant in self.browse(cr, uid, quant_ids, context=context):
            move = quant._get_latest_move()
            valuation_update = newprice - quant.cost
            # this is where we post accounting entries for adjustment, if needed
            if not quant.company_id.currency_id.is_zero(valuation_update):
                # If neg quant period already closed (likely with manual valuation), skip update
                if account_move_obj._check_lock_date(cr, uid, [move.id], context=context):
                    ctx = dict(context, force_valuation_amount=valuation_update)
                    self._account_entry_move(cr, uid, [quant], move, context=ctx)

            #update the standard price of the product, only if we would have done it if we'd have had enough stock at first, which means
            #1) the product cost's method is 'real'
            #2) we just fixed a negative quant caused by an outgoing shipment
            if quant.product_id.cost_method == 'real' and quant.location_id.usage != 'internal':
                self.pool.get('stock.move')._store_average_cost_price(cr, uid, move, context=context)

    def _account_entry_move(self, cr, uid, ids, move, context=None):
        """
        Accounting Valuation Entries

        quants: browse record list of Quants to create accounting valuation entries for. Unempty and all quants are supposed to have the same location id (thay already moved in)
        move: Move to use. browse record
        """
        quants = self.browse(cr, uid, ids, context=context)
        if context is None:
            context = {}
        location_obj = self.pool.get('stock.location')
        location_from = move.location_id
        location_to = quants[0].location_id
        company_from = location_from and (location_from.usage == 'internal') and location_from.company_id or False
        company_to = location_to and (location_to.usage == 'internal') and location_to.company_id or False

        if move.product_id.valuation != 'real_time':
            return False
        if move.product_id.type != 'product':
            #No stock valuation for consumable products
            return False
        for q in quants:
            if q.owner_id:
                #if the quant isn't owned by the company, we don't make any valuation entry
                return False
            if q.qty <= 0:
                #we don't make any stock valuation for negative quants because the valuation is already made for the counterpart.
                #At that time the valuation will be made at the product cost price and afterward there will be new accounting entries
                #to make the adjustments when we know the real cost price.
                return False

        #in case of routes making the link between several warehouse of the same company, the transit location belongs to this company, so we don't need to create accounting entries
        # Create Journal Entry for products arriving in the company
        if company_to and (move.location_id.usage not in ('internal', 'transit') and move.location_dest_id.usage == 'internal' or company_from != company_to):
            ctx = context.copy()
            ctx['force_company'] = company_to.id
            journal_id, acc_src, acc_dest, acc_valuation = self._get_accounting_data_for_valuation(cr, uid, move, context=ctx)
            if location_from and location_from.usage == 'customer':
                #goods returned from customer
                self._create_account_move_line(cr, uid, quants, move, acc_dest, acc_valuation, journal_id, context=ctx)
            else:
                self._create_account_move_line(cr, uid, quants, move, acc_src, acc_valuation, journal_id, context=ctx)

        # Create Journal Entry for products leaving the company
        if company_from and (move.location_id.usage == 'internal' and move.location_dest_id.usage not in ('internal', 'transit') or company_from != company_to):
            ctx = context.copy()
            ctx['force_company'] = company_from.id
            journal_id, acc_src, acc_dest, acc_valuation = self._get_accounting_data_for_valuation(cr, uid, move, context=ctx)
            if location_to and location_to.usage == 'supplier':
                #goods returned to supplier
                self._create_account_move_line(cr, uid, quants, move, acc_valuation, acc_src, journal_id, context=ctx)
            else:
                self._create_account_move_line(cr, uid, quants, move, acc_valuation, acc_dest, journal_id, context=ctx)

    def _quant_create_from_move(self, cr, uid, qty, move, lot_id=False, owner_id=False, src_package_id=False, dest_package_id=False, force_location_from=False, force_location_to=False, context=None):
        quant_obj = self.pool.get('stock.quant')
        quant = super(stock_quant, self)._quant_create_from_move(cr, uid, qty, move, lot_id=lot_id, owner_id=owner_id, src_package_id=src_package_id, dest_package_id=dest_package_id, force_location_from=force_location_from, force_location_to=force_location_to, context=context)
        quant._account_entry_move(move)
        if move.product_id.valuation == 'real_time':
            # If the precision required for the variable quant cost is larger than the accounting
            # precision, inconsistencies between the stock valuation and the accounting entries
            # may arise.
            # For example, a box of 13 units is bought 15.00. If the products leave the
            # stock one unit at a time, the amount related to the cost will correspond to
            # round(15/13, 2)*13 = 14.95. To avoid this case, we split the quant in 12 + 1, then
            # record the difference on the new quant.
            # We need to make sure to able to extract at least one unit of the product. There is
            # an arbitrary minimum quantity set to 2.0 from which we consider we can extract a
            # unit and adapt the cost.
            curr_rounding = move.company_id.currency_id.rounding
            cost_rounded = float_round(quant.cost, precision_rounding=curr_rounding)
            cost_correct = cost_rounded
            if float_compare(quant.product_id.uom_id.rounding, 1.0, precision_digits=1) == 0\
                    and float_compare(quant.qty * quant.cost, quant.qty * cost_rounded, precision_rounding=curr_rounding) != 0\
                    and float_compare(quant.qty, 2.0, precision_rounding=quant.product_id.uom_id.rounding) >= 0:
                qty = quant.qty
                cost = quant.cost
                quant_correct = quant._quant_split(quant.qty - 1.0)
                cost_correct += (qty * cost) - (qty * cost_rounded)
                quant_obj.write(cr, SUPERUSER_ID, [quant.id], {'cost': cost_rounded}, context=context)
                quant_obj.write(cr, SUPERUSER_ID, [quant_correct.id], {'cost': cost_correct}, context=context)
        return quant

    @api.multi
    def _quant_update_from_move(self, move, location_dest_id, dest_package_id, lot_id=False, entire_pack=False):
        res = super(stock_quant, self)._quant_update_from_move(move, location_dest_id, dest_package_id, lot_id=lot_id, entire_pack=entire_pack)
        self._account_entry_move(move)
        return res

    def _get_accounting_data_for_valuation(self, cr, uid, move, context=None):
        """
        Return the accounts and journal to use to post Journal Entries for the real-time
        valuation of the quant.

        :param context: context dictionary that can explicitly mention the company to consider via the 'force_company' key
        :returns: journal_id, source account, destination account, valuation account
        :raise: openerp.exceptions.UserError if any mandatory account or journal is not defined.
        """
        product_obj = self.pool.get('product.template')
        accounts = product_obj.browse(cr, uid, move.product_id.product_tmpl_id.id, context).get_product_accounts()
        if move.location_id.valuation_out_account_id:
            acc_src = move.location_id.valuation_out_account_id.id
        else:
            acc_src = accounts['stock_input'].id

        if move.location_dest_id.valuation_in_account_id:
            acc_dest = move.location_dest_id.valuation_in_account_id.id
        else:
            acc_dest = accounts['stock_output'].id

        acc_valuation = accounts.get('stock_valuation', False)
        if acc_valuation:
            acc_valuation = acc_valuation.id
        if not accounts.get('stock_journal', False):
            raise UserError(_('You don\'t have any stock journal defined on your product category, check if you have installed a chart of accounts'))
        if not acc_src:
            raise UserError(_('Cannot find a stock input account for the product %s. You must define one on the product category, or on the location, before processing this operation.') % (move.product_id.name))
        if not acc_dest:
            raise UserError(_('Cannot find a stock output account for the product %s. You must define one on the product category, or on the location, before processing this operation.') % (move.product_id.name))
        if not acc_valuation:
            raise UserError(_('You don\'t have any stock valuation account defined on your product category. You must define one before processing this operation.'))
        journal_id = accounts['stock_journal'].id
        return journal_id, acc_src, acc_dest, acc_valuation

    def _prepare_account_move_line(self, cr, uid, move, qty, cost, credit_account_id, debit_account_id, context=None):
        """
        Generate the account.move.line values to post to track the stock valuation difference due to the
        processing of the given quant.
        """
        if context is None:
            context = {}
        currency_obj = self.pool.get('res.currency')
        if context.get('force_valuation_amount'):
            valuation_amount = context.get('force_valuation_amount')
        else:
            if move.product_id.cost_method == 'average':
                valuation_amount = cost if move.location_id.usage == 'supplier' and move.location_dest_id.usage == 'internal' else move.product_id.standard_price
            else:
                valuation_amount = cost if move.product_id.cost_method == 'real' else move.product_id.standard_price
        #the standard_price of the product may be in another decimal precision, or not compatible with the coinage of
        #the company currency... so we need to use round() before creating the accounting entries.
        debit_value = currency_obj.round(cr, uid, move.company_id.currency_id, valuation_amount * qty)
        #check that all data is correct
        if move.company_id.currency_id.is_zero(debit_value):
            raise UserError(_("The found valuation amount for product %s is zero. Which means there is probably a configuration error. Check the costing method and the standard price") % (move.product_id.name,))
        credit_value = debit_value

        if move.product_id.cost_method == 'average' and move.company_id.anglo_saxon_accounting:
            #in case of a supplier return in anglo saxon mode, for products in average costing method, the stock_input
            #account books the real purchase price, while the stock account books the average price. The difference is
            #booked in the dedicated price difference account.
            if move.location_dest_id.usage == 'supplier' and move.origin_returned_move_id and move.origin_returned_move_id.purchase_line_id:
                debit_value = move.origin_returned_move_id.price_unit * qty
            #in case of a customer return in anglo saxon mode, for products in average costing method, the stock valuation
            #is made using the original average price to negate the delivery effect.
            if move.location_id.usage == 'customer' and move.origin_returned_move_id:
                debit_value = move.origin_returned_move_id.price_unit * qty
                credit_value = debit_value
        partner_id = (move.picking_id.partner_id and self.pool.get('res.partner')._find_accounting_partner(move.picking_id.partner_id).id) or False
        debit_line_vals = {
                    'name': move.name,
                    'product_id': move.product_id.id,
                    'quantity': qty,
                    'product_uom_id': move.product_id.uom_id.id,
                    'ref': move.picking_id and move.picking_id.name or False,
                    'partner_id': partner_id,
                    'debit': debit_value,
                    'credit': 0,
                    'account_id': debit_account_id,
        }
        credit_line_vals = {
                    'name': move.name,
                    'product_id': move.product_id.id,
                    'quantity': qty,
                    'product_uom_id': move.product_id.uom_id.id,
                    'ref': move.picking_id and move.picking_id.name or False,
                    'partner_id': partner_id,
                    'credit': credit_value,
                    'debit': 0,
                    'account_id': credit_account_id,
        }
        res = [(0, 0, debit_line_vals), (0, 0, credit_line_vals)]
        if credit_value != debit_value:
            #for supplier returns of product in average costing method, in anglo saxon mode
            diff_amount = debit_value - credit_value
            price_diff_account = move.product_id.property_account_creditor_price_difference
            if not price_diff_account:
                price_diff_account = move.product_id.categ_id.property_account_creditor_price_difference_categ
            if not price_diff_account:
                raise UserError(_('Configuration error. Please configure the price difference account on the product or its category to process this operation.'))
            price_diff_line = {
                    'name': move.name,
                    'product_id': move.product_id.id,
                    'quantity': qty,
                    'product_uom_id': move.product_id.uom_id.id,
                    'ref': move.picking_id and move.picking_id.name or False,
                    'partner_id': partner_id,
                    'credit': diff_amount > 0 and diff_amount or 0,
                    'debit': diff_amount < 0 and -diff_amount or 0,
                    'account_id': price_diff_account.id,
            }
            res.append((0, 0, price_diff_line))
        return res

    def _create_account_move_line(self, cr, uid, quants, move, credit_account_id, debit_account_id, journal_id, context=None):
        #group quants by cost
        quant_cost_qty = {}
        for quant in quants:
            if quant_cost_qty.get(quant.cost):
                quant_cost_qty[quant.cost] += quant.qty
            else:
                quant_cost_qty[quant.cost] = quant.qty
        move_obj = self.pool.get('account.move')
        for cost, qty in quant_cost_qty.items():
            move_lines = self._prepare_account_move_line(cr, uid, move, qty, cost, credit_account_id, debit_account_id, context=context)
            date = context.get('force_period_date', fields.date.context_today(self, cr, uid, context=context))
            new_move = move_obj.create(cr, uid, {'journal_id': journal_id,
                                      'line_ids': move_lines,
                                      'date': date,
                                      'ref': move.picking_id.name}, context=context)
            move_obj.post(cr, uid, [new_move], context=context)

    #def _reconcile_single_negative_quant(self, cr, uid, to_solve_quant, quant, quant_neg, qty, context=None):
    #    move = self._get_latest_move(cr, uid, to_solve_quant, context=context)
    #    quant_neg_position = quant_neg.negative_dest_location_id.usage
    #    remaining_solving_quant, remaining_to_solve_quant = super(stock_quant, self)._reconcile_single_negative_quant(cr, uid, to_solve_quant, quant, quant_neg, qty, context=context)
    #    #update the standard price of the product, only if we would have done it if we'd have had enough stock at first, which means
    #    #1) there isn't any negative quant anymore
    #    #2) the product cost's method is 'real'
    #    #3) we just fixed a negative quant caused by an outgoing shipment
    #    if not remaining_to_solve_quant and move.product_id.cost_method == 'real' and quant_neg_position != 'internal':
    #        self.pool.get('stock.move')._store_average_cost_price(cr, uid, move, context=context)
    #    return remaining_solving_quant, remaining_to_solve_quant

class stock_move(osv.osv):
    _inherit = "stock.move"

    def action_done(self, cr, uid, ids, context=None):
        self.product_price_update_before_done(cr, uid, ids, context=context)
        res = super(stock_move, self).action_done(cr, uid, ids, context=context)
        self.product_price_update_after_done(cr, uid, ids, context=context)
        return res

    def _store_average_cost_price(self, cr, uid, move, context=None):
        ''' move is a browe record '''
        product_obj = self.pool.get('product.product')
        if any([q.qty <= 0 for q in move.quant_ids]) or move.product_qty == 0:
            #if there is a negative quant, the standard price shouldn't be updated
            return
        #Note: here we can't store a quant.cost directly as we may have moved out 2 units (1 unit to 5€ and 1 unit to 7€) and in case of a product return of 1 unit, we can't know which of the 2 costs has to be used (5€ or 7€?). So at that time, thanks to the average valuation price we are storing we will valuate it at 6€
        average_valuation_price = 0.0
        for q in move.quant_ids:
            average_valuation_price += q.qty * q.cost
        average_valuation_price = average_valuation_price / move.product_qty
        # Write the standard price, as SUPERUSER_ID because a warehouse manager may not have the right to write on products
        ctx = dict(context or {}, force_company=move.company_id.id)
        product_obj.write(cr, SUPERUSER_ID, [move.product_id.id], {'standard_price': average_valuation_price}, context=ctx)
        self.write(cr, uid, [move.id], {'price_unit': average_valuation_price}, context=context)

    def product_price_update_before_done(self, cr, uid, ids, context=None):
        product_obj = self.pool.get('product.product')
        tmpl_dict = {}
        for move in self.browse(cr, uid, ids, context=context):
            #adapt standard price on incomming moves if the product cost_method is 'average'
            if (move.location_id.usage == 'supplier') and (move.product_id.cost_method == 'average'):
                product = move.product_id
                product_id = move.product_id.id
                qty_available = move.product_id.qty_available
                if tmpl_dict.get(product_id):
                    product_avail = qty_available + tmpl_dict[product_id]
                else:
                    tmpl_dict[product_id] = 0
                    product_avail = qty_available
                # if the incoming move is for a purchase order with foreign currency, need to call this to get the same value that the quant will use.
                price_unit = move.get_price_unit()
                if product_avail <= 0:
                    new_std_price = price_unit
                else:
                    # Get the standard price
                    amount_unit = product.standard_price
                    new_std_price = ((amount_unit * product_avail) + (price_unit * move.product_qty)) / (product_avail + move.product_qty)
                tmpl_dict[product_id] += move.product_qty
                # Write the standard price, as SUPERUSER_ID because a warehouse manager may not have the right to write on products
                ctx = dict(context or {}, force_company=move.company_id.id)
                product_obj.write(cr, SUPERUSER_ID, [product.id], {'standard_price': new_std_price}, context=ctx)

    def product_price_update_after_done(self, cr, uid, ids, context=None):
        '''
        This method adapts the price on the product when necessary
        '''
        for move in self.browse(cr, uid, ids, context=context):
            #adapt standard price on outgoing moves if the product cost_method is 'real', so that a return
            #or an inventory loss is made using the last value used for an outgoing valuation.
            if move.product_id.cost_method == 'real' and move.location_dest_id.usage != 'internal':
                #store the average price of the move on the move and product form
                self._store_average_cost_price(cr, uid, move, context=context)

class AccountChartTemplate(models.Model):
    _inherit = "account.chart.template"

    @api.model
    def generate_journals(self, acc_template_ref, company, journals_dict=None):
        journal_to_add = [{'name': _('Stock Journal'), 'type': 'general', 'code': 'STJ', 'favorite': False, 'sequence': 8}]
        super(AccountChartTemplate, self).generate_journals(acc_template_ref=acc_template_ref, company=company, journals_dict=journal_to_add)

    @api.multi
    def generate_properties(self, acc_template_ref, company, property_list=None):
        super(AccountChartTemplate, self).generate_properties(acc_template_ref=acc_template_ref, company=company)
        PropertyObj = self.env['ir.property']  # Property Stock Journal
        value = self.env['account.journal'].search([('company_id', '=', company.id), ('code', '=', 'STJ'), ('type', '=', 'general')], limit=1)
        if value:
            field = self.env['ir.model.fields'].search([('name', '=', 'property_stock_journal'), ('model', '=', 'product.category'), ('relation', '=', 'account.journal')], limit=1)
            vals = {
                'name': 'property_stock_journal',
                'company_id': company.id,
                'fields_id': field.id,
                'value': 'account.journal,%s' % value.id,
            }
            properties = PropertyObj.search([('name', '=', 'property_stock_journal'), ('company_id', '=', company.id)])
            if properties:
                #the property exist: modify it
                properties.write(vals)
            else:
                #create the property
                PropertyObj.create(vals)

        todo_list = [  # Property Stock Accounts
            'property_stock_account_input_categ_id',
            'property_stock_account_output_categ_id',
            'property_stock_valuation_account_id',
        ]
        for record in todo_list:
            account = getattr(self, record)
            value = account and 'account.account,' + str(acc_template_ref[account.id]) or False
            if value:
                field = self.env['ir.model.fields'].search([('name', '=', record), ('model', '=', 'product.category'), ('relation', '=', 'account.account')], limit=1)
                vals = {
                    'name': record,
                    'company_id': company.id,
                    'fields_id': field.id,
                    'value': value,
                }
                properties = PropertyObj.search([('name', '=', record), ('company_id', '=', company.id)])
                if properties:
                    #the property exist: modify it
                    properties.write(vals)
                else:
                    #create the property
                    PropertyObj.create(vals)

        return True
